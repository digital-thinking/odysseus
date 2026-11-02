"""Background indexing job for library sources (one at a time, in-process).

Phases: ``scanning`` (rows + thumbnails) → ``embedding`` (smart-search
vectors) → ``tagging`` (AI tags via the tagging model). Each phase only works
on what is new or missing, so re-running a scan is cheap.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_job: Optional[dict] = None
_cancel = threading.Event()
_MAX_CONSECUTIVE_TAG_FAILURES = 5
_WAIT_POLL_SECONDS = 5   # how often the scheduled task checks whether a manual scan has finished


class JobAlreadyRunning(RuntimeError):
    pass


def status() -> Optional[dict]:
    with _lock:
        return dict(_job) if _job else None


def is_running(source_id: Optional[str] = None) -> bool:
    with _lock:
        if not _job or _job["state"] != "running":
            return False
        return source_id is None or _job["source_id"] == source_id


def cancel() -> bool:
    if not is_running():
        return False
    _cancel.set()
    return True


def _claim(source_id: str, embed: bool, tag: bool, trigger: str, faces: bool = False) -> Optional[dict]:
    """Register a new running job, or return None if one is already running."""
    global _job
    with _lock:
        if _job and _job["state"] == "running":
            return None
        _cancel.clear()
        _job = {
            "id": uuid.uuid4().hex[:12],
            "source_id": source_id,
            "state": "running",
            "phase": "starting",
            "done": 0,
            "total": 0,
            "options": {"embed": bool(embed), "tag": bool(tag), "faces": bool(faces)},
            "trigger": trigger,  # manual | task
            "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "finished_at": None,
            "result": {},
            "error": None,
        }
        return dict(_job)


def start(source_id: str, *, embed: bool = True, tag: bool = False, faces: bool = False,
          trigger: str = "manual") -> dict:
    """Start a job in a background thread (the Scan now button)."""
    job = _claim(source_id, embed, tag, trigger, faces)
    if job is None:
        raise JobAlreadyRunning("An indexing job is already running")
    threading.Thread(target=_run, args=(source_id, bool(embed), bool(tag), bool(faces)), daemon=True,
                     name="gallery-library-index").start()
    return job


def run_now(source_id: str, *, embed: bool = True, tag: bool = False, faces: bool = False, trigger: str = "task",
            wait_seconds: float = 6 * 3600, stop: Optional[threading.Event] = None) -> dict:
    """Run a job in the calling thread (the scheduled task) and return its final status.

    If a manual scan is running, waits for it first instead of failing.
    Setting ``stop`` (the task was cancelled) ends the wait without starting a
    job, or stops this call's job if it has already started.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        if stop is not None and stop.is_set():
            return {"state": "cancelled", "source_id": source_id, "trigger": trigger, "result": {}}
        if _claim(source_id, embed, tag, trigger, faces) is not None:
            break
        if time.monotonic() > deadline:
            raise JobAlreadyRunning("Another indexing job kept running")
        if stop is not None:
            stop.wait(_WAIT_POLL_SECONDS)
        else:
            time.sleep(_WAIT_POLL_SECONDS)
    if stop is not None and stop.is_set():
        _cancel.set()   # cancelled right as the job was claimed: stop it at once
    _run(source_id, bool(embed), bool(tag), bool(faces))
    return status() or {}


def _update(**fields) -> None:
    with _lock:
        if _job is not None:
            _job.update(fields)


def _progress(phase: str, done: int, total: int) -> None:
    _update(phase=phase, done=done, total=total)


def _run(source_id: str, embed: bool, tag: bool, faces: bool = False) -> None:
    from src.gallery_library.scanner import scan_source

    result: dict = {}
    try:
        scan = scan_source(source_id, progress=_progress, cancel=_cancel)
        result["scan"] = scan["stats"]
        if scan["changed_ids"]:
            # New or changed photos: drop any stale vector so the embedding
            # phase (now or on the next run) indexes the current content.
            from src.gallery_library import embeddings
            embeddings.delete_ids(scan["changed_ids"])
        if embed and not _cancel.is_set():
            result["embedding"] = _embed_source(source_id)
        if faces and not _cancel.is_set():
            result["faces"] = _faces_source(source_id)
        if tag and not _cancel.is_set():
            result["tagging"] = _tag_source(source_id)
        state = "cancelled" if _cancel.is_set() else "done"
        _update(state=state, phase=state, result=result)
    except Exception as exc:
        logger.exception("Library indexing failed")
        _update(state="error", phase="error", error=str(exc), result=result)
    finally:
        _update(finished_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        _store_job_result(source_id, result)


def _faces_source(source_id: str) -> dict:
    """Find faces in new photos, then (re)group faces into people."""
    from core.database import GalleryLibrarySource, SessionLocal
    from src.gallery_faces.index import cluster_faces, scan_faces

    started = time.time()
    _progress("faces", 0, 0)
    stats = scan_faces(source_id, progress=lambda d, t: _progress("faces", d, t), cancel=_cancel)
    db = SessionLocal()
    try:
        source = db.query(GalleryLibrarySource).filter(GalleryLibrarySource.id == source_id).first()
        owner = source.owner if source else None
    finally:
        db.close()
    if not _cancel.is_set():
        stats.update(cluster_faces(owner))
    stats["seconds"] = round(time.time() - started, 1)
    return stats


def _active_rows(db, source_id: str):
    from core.database import GalleryImage
    return db.query(GalleryImage).filter(
        GalleryImage.source_id == source_id,
        GalleryImage.is_active == True,  # noqa: E712
    )


def _embed_source(source_id: str) -> dict:
    from core.database import SessionLocal
    from src.gallery_library import embeddings
    from src.gallery_library.files import thumb_path

    db = SessionLocal()
    try:
        rows = [(r.id, r.owner, r.source_id) for r in _active_rows(db, source_id)]
    finally:
        db.close()
    _progress("embedding", 0, 0)
    todo_ids = set(embeddings.missing_ids([r[0] for r in rows]))
    items = [(i, thumb_path(i), owner or "", sid) for i, owner, sid in rows
             if i in todo_ids and os.path.exists(thumb_path(i))]
    started = time.time()
    _progress("embedding", 0, len(items))
    stored = embeddings.index_images(items, progress=lambda d, t: _progress("embedding", d, t), cancel=_cancel)
    # Load (and on first use download) the query model now, so the first
    # smart search does not have to wait for it.
    _progress("embedding", len(items), len(items))
    ready = embeddings.warm_up(wait_seconds=900)
    return {"embedded": stored, "already_indexed": len(rows) - len(todo_ids),
            "seconds": round(time.time() - started, 1), "search_model_ready": ready}


def _tag_source(source_id: str) -> dict:
    from core.database import SessionLocal
    from src.gallery_library.files import thumb_path
    from src.gallery_tagging import TaggingUnavailable, request_tags

    db = SessionLocal()
    try:
        todo = [r.id for r in _active_rows(db, source_id).filter(_ai_tags_empty())]
    finally:
        db.close()
    started = time.time()
    tagged = failed = consecutive = 0
    _progress("tagging", 0, len(todo))
    for index, image_id in enumerate(todo, start=1):
        if _cancel.is_set():
            break
        db = SessionLocal()
        try:
            from core.database import GalleryImage
            img = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
            if img is None:
                continue
            # The 512px thumbnail is plenty for tagging and much cheaper than the original.
            with open(thumb_path(image_id), "rb") as fh:
                tags = request_tags(fh.read(), "image/jpeg", img.owner)
            if tags:
                img.ai_tags = tags
                db.commit()
                tagged += 1
            consecutive = 0
        except TaggingUnavailable as exc:
            return {"tagged": tagged, "failed": failed, "error": str(exc)}
        except Exception as exc:  # TaggingFailed, unreadable thumbnail, network
            failed += 1
            consecutive += 1
            logger.warning("Tagging %s failed: %s", image_id, exc)
            if consecutive >= _MAX_CONSECUTIVE_TAG_FAILURES:
                return {"tagged": tagged, "failed": failed,
                        "error": f"Stopped after {consecutive} failures in a row: {exc}"}
        finally:
            db.close()
        _progress("tagging", index, len(todo))
    seconds = time.time() - started
    return {"tagged": tagged, "failed": failed, "seconds": round(seconds, 1),
            "seconds_per_photo": round(seconds / max(1, tagged + failed), 2)}


def _ai_tags_empty():
    from core.database import GalleryImage
    return (GalleryImage.ai_tags == None) | (GalleryImage.ai_tags == "")  # noqa: E711


def _store_job_result(source_id: str, result: dict) -> None:
    """Merge embedding/tagging results into the source's last_scan_stats."""
    if not result:
        return
    from core.database import GalleryLibrarySource, SessionLocal
    db = SessionLocal()
    try:
        source = db.query(GalleryLibrarySource).filter(GalleryLibrarySource.id == source_id).first()
        if source is None:
            return
        stats = dict(source.last_scan_stats or {})
        for key in ("embedding", "faces", "tagging"):
            if key in result:
                stats[key] = result[key]
        source.last_scan_stats = stats
        db.commit()
    except Exception as exc:
        logger.warning("Could not store library job result: %s", exc)
    finally:
        db.close()

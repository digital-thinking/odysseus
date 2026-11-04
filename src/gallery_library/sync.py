"""Keeping library folders in sync: per-folder options and the scheduled task.

New photos are picked up by the built-in scheduled task "Gallery Library
Index" (action ``index_gallery_library``, nightly by default, pausable and
cron-editable in Tasks). Rescanning is cheap — unchanged files are skipped by
size and mtime — so only new or changed photos get thumbnails, smart-search
vectors and AI tags.

Scheduled rescans rather than file watching on purpose: changes made on a
Windows host (or by a cloud-drive client such as HiDrive) do not reach a
Docker bind mount as inotify events, and network shares rarely deliver them.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

TASK_ACTION = "index_gallery_library"
DEFAULT_OPTIONS: dict[str, Any] = {"embed": True, "tag": False, "faces": False, "albums": False}
_PROGRESS_POLL_SECONDS = 2


def sync_settings(source) -> dict[str, Any]:
    """What the index task does for new photos of a folder (smart search, AI tags)."""
    return normalize_sync_settings(getattr(source, "sync_settings", None) or {}, base=DEFAULT_OPTIONS)


def normalize_sync_settings(raw: Any, base: Optional[dict] = None) -> dict[str, Any]:
    """Validate a (partial) options dict; unknown keys are dropped."""
    out = {key: bool(value) for key, value in (base or DEFAULT_OPTIONS).items() if key in DEFAULT_OPTIONS}
    if isinstance(raw, dict):
        for key in DEFAULT_OPTIONS:
            if key in raw:
                out[key] = bool(raw[key])
    return out


def index_task_info(owner: Optional[str]) -> Optional[dict]:
    """The owner's "Gallery Library Index" task (schedule, status, next run), if seeded."""
    from core.database import ScheduledTask, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(ScheduledTask).filter(ScheduledTask.task_type == "action", ScheduledTask.action == TASK_ACTION)
        if owner is not None:
            q = q.filter(ScheduledTask.owner == owner)
        task = q.first()
        if task is None:
            return None
        return {
            "id": task.id,
            "name": task.name,
            "status": task.status,
            "cron_expression": task.cron_expression,
            "next_run": task.next_run.isoformat() + "Z" if task.next_run else None,
            "last_run": task.last_run.isoformat() + "Z" if task.last_run else None,
        }
    finally:
        db.close()


def _summary(name: str, job: dict) -> tuple[str, bool, bool]:
    """(line, did_work, ok) for one folder's job result."""
    result = job.get("result") or {}
    if job.get("state") == "error":
        return f"{name}: failed — {job.get('error')}", True, False
    scan = result.get("scan") or {}
    bits = []
    for key, label in (("new", "new"), ("updated", "changed"), ("missing", "gone from the folder"),
                       ("reactivated", "back"), ("errors", "unreadable")):
        if scan.get(key):
            bits.append(f"{scan[key]} {label}")
    albums = (result.get("albums") or {}).get("created") or 0
    if albums:
        bits.append(f"{albums} new album{'' if albums == 1 else 's'}")
    embedded = (result.get("embedding") or {}).get("embedded") or 0
    if embedded:
        bits.append(f"{embedded} added to smart search")
    faces = result.get("faces") or {}
    if faces.get("faces"):
        bits.append(f"{faces['faces']} faces found")
    if faces.get("matched"):
        bits.append(f"{faces['matched']} recognised")
    tagging = result.get("tagging") or {}
    if tagging.get("tagged"):
        bits.append(f"{tagging['tagged']} AI-tagged")
    if tagging.get("error"):
        bits.append(f"tagging stopped: {tagging['error']}")
    did_work = bool(bits)
    ok = not tagging.get("error") and not scan.get("errors")
    return f"{name}: {', '.join(bits) if bits else 'up to date'}", did_work, ok


async def run_index_task(owner: str, progress_cb: Optional[Callable[[str], None]] = None) -> tuple[str, bool]:
    """Index every enabled library folder of ``owner`` (the scheduled task's work)."""
    from core.database import GalleryLibrarySource, SessionLocal
    from src.builtin_actions import TaskNoop
    from src.gallery_library import jobs

    db = SessionLocal()
    try:
        sources = [
            (s.id, s.name, sync_settings(s))
            for s in db.query(GalleryLibrarySource).filter(
                GalleryLibrarySource.owner == owner,
                GalleryLibrarySource.enabled == True,  # noqa: E712
            ).order_by(GalleryLibrarySource.created_at)
        ]
    finally:
        db.close()
    if not sources:
        raise TaskNoop("no library folders")

    lines, worked, all_ok = [], False, True
    for source_id, name, options in sources:
        stop = threading.Event()
        run = asyncio.ensure_future(asyncio.to_thread(
            jobs.run_now, source_id, embed=options["embed"], tag=options["tag"], faces=options["faces"],
            trigger="task", stop=stop))
        try:
            while not run.done():
                current = jobs.status()
                if progress_cb and current and current.get("source_id") == source_id and current.get("state") == "running":
                    total = current.get("total") or 0
                    progress_cb(f"{name}: {current.get('phase')}" + (f" {current.get('done')}/{total}" if total else ""))
                await asyncio.sleep(_PROGRESS_POLL_SECONDS)
        except asyncio.CancelledError:
            # The scheduler stops background work when Odysseus is used (or the
            # user stops the run): stop the indexing thread too, at its next
            # photo / batch. The next run continues where this one stopped.
            # Only this task's own job: a manual scan it was waiting for keeps going.
            stop.set()
            current = jobs.status() or {}
            if (current.get("state") == "running" and current.get("source_id") == source_id
                    and current.get("trigger") == "task"):
                jobs.cancel()
            raise
        line, did_work, ok = _summary(name, run.result())
        lines.append(line)
        worked = worked or did_work
        all_ok = all_ok and ok
    if not worked:
        raise TaskNoop("; ".join(lines))
    return "\n".join(lines), all_ok

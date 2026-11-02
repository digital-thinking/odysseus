"""Index a library source in place: gallery rows + thumbnails.

Source files are only ever opened with ``"rb"``. Everything written goes to
Odysseus's data directory (rows in the DB, thumbnails under
``data/gallery_library/thumbs``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Optional

from src.gallery_library.files import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    library_filename,
    thumb_path,
    write_thumbnail,
)
from src.gallery_library.paths import confine_to_source, is_inside_library
from src.index_walk import is_indexable_file, prune_index_dirs

logger = logging.getLogger(__name__)

Progress = Callable[[str, int, int], None]  # (phase, done, total)

_COMMIT_EVERY = 50
_MAX_ERROR_SAMPLES = 10


def normalize_include_paths(values) -> list[str]:
    """Relative subfolders as clean POSIX paths ("2005", "2006/Urlaub"); empty = whole tree."""
    if isinstance(values, str):
        values = [v for v in values.replace(";", ",").replace("\n", ",").split(",")]
    out: list[str] = []
    for value in values or []:
        text = str(value or "").strip().replace("\\", "/").strip("/")
        if not text or text == ".":
            continue
        parts = [p for p in text.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError(f"Subfolder must stay inside the library folder: {value}")
        path = "/".join(parts)
        if path not in out:
            out.append(path)
    return out


def _walk(root: str, include_paths: list[str]) -> Iterator[tuple[str, str]]:
    """Yield (relpath, absolute path) for indexable files below the included subfolders."""
    seen: set[str] = set()
    for include in include_paths or [""]:
        base = confine_to_source(root, include)
        if not os.path.isdir(base):
            raise FileNotFoundError(f"Subfolder not found: {include}")
        for dirpath, dirs, files in os.walk(base, followlinks=False):
            prune_index_dirs(dirs)
            dirs.sort()
            for name in sorted(files):
                if not is_indexable_file(name):
                    continue
                abs_path = os.path.join(dirpath, name)
                rel = PurePosixPath(*Path(os.path.relpath(abs_path, root)).parts).as_posix()
                if rel not in seen:
                    seen.add(rel)
                    yield rel, abs_path


def scan_source(
    source_id: str,
    *,
    progress: Optional[Progress] = None,
    cancel: Optional[threading.Event] = None,
) -> dict:
    """Bring the index of one source up to date. Returns stats and changed image ids."""
    from core.database import GalleryImage, GalleryLibrarySource, SessionLocal, utcnow_naive
    from routes.gallery.gallery_helpers import _extract_exif

    progress = progress or (lambda *_: None)
    db = SessionLocal()
    try:
        source = db.query(GalleryLibrarySource).filter(GalleryLibrarySource.id == source_id).first()
        if source is None:
            raise ValueError("Library source not found")
        root = source.path
        if not is_inside_library(root):
            raise ValueError("This folder is no longer inside a configured library mount")
        if not os.path.isdir(root):
            raise FileNotFoundError(f"Library folder not reachable: {source.display_path or root}")
        includes = normalize_include_paths(source.include_paths)

        stats = {"found": 0, "new": 0, "updated": 0, "unchanged": 0, "reactivated": 0,
                 "missing": 0, "skipped_videos": 0, "skipped_other": 0, "errors": 0}
        error_samples: list[str] = []

        progress("listing", 0, 0)
        images: list[tuple[str, str]] = []
        for rel, abs_path in _walk(root, includes):
            ext = os.path.splitext(rel)[1].lower()
            if ext in IMAGE_EXTS:
                images.append((rel, abs_path))
            elif ext in VIDEO_EXTS:
                stats["skipped_videos"] += 1
            else:
                stats["skipped_other"] += 1
        stats["found"] = len(images)

        existing = {
            row.source_relpath: row
            for row in db.query(GalleryImage).filter(GalleryImage.source_id == source.id)
        }
        prefixes = [inc + "/" for inc in includes]
        indexed_here = sum(
            1 for rel, row in existing.items()
            if row.source_state == "ok" and (not prefixes or any(rel.startswith(p) for p in prefixes))
        )
        if not images and indexed_here:
            # A drive that is offline / not mounted often shows up as an empty
            # folder. Don't turn that into "every photo is gone".
            raise FileNotFoundError(
                f"{source.display_path or root} looks empty although {indexed_here} photos are indexed "
                "— is the drive connected? Nothing was changed."
            )
        changed_ids: list[str] = []
        cancelled = False
        total = len(images)
        for index, (rel, abs_path) in enumerate(images, start=1):
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            try:
                row = existing.get(rel)
                if row is not None and row.source_state == "hidden":
                    stats["unchanged"] += 1
                    continue
                st = os.stat(abs_path)
                if (row is not None and row.source_mtime == st.st_mtime
                        and row.file_size == st.st_size and os.path.exists(thumb_path(row.id))):
                    if row.source_state != "ok" or not row.is_active:
                        row.source_state, row.is_active = "ok", True
                        stats["reactivated"] += 1
                    else:
                        stats["unchanged"] += 1
                    continue

                with open(abs_path, "rb") as fh:  # the only way a source file is ever opened
                    content = fh.read()
                image_id = row.id if row is not None else str(uuid.uuid4())
                width, height = write_thumbnail(image_id, content)
                exif = _extract_exif(content, with_dimensions=False)
                file_hash = hashlib.sha256(content).hexdigest()
                fields = {
                    "file_hash": file_hash,
                    "file_size": st.st_size,
                    "width": width,
                    "height": height,
                    "size": f"{width}x{height}",
                    # Old phone photos often carry no EXIF date; the file time is the capture time.
                    "taken_at": exif.get("taken_at") or datetime.fromtimestamp(st.st_mtime),
                    "camera_make": exif.get("camera_make"),
                    "camera_model": exif.get("camera_model"),
                    "gps_lat": exif.get("gps_lat"),
                    "gps_lng": exif.get("gps_lng"),
                    "source_mtime": st.st_mtime,
                    "source_state": "ok",
                    "is_active": True,
                }
                if row is not None:
                    content_changed = row.file_hash != file_hash
                    for key, value in fields.items():
                        setattr(row, key, value)
                    if not content_changed:
                        stats["unchanged"] += 1  # only the mtime or the thumbnail needed refreshing
                        continue
                    row.ai_tags = ""  # new content: re-tag it (the user's own tags stay)
                    row.faces_scanned = None  # ... and look for faces again
                    stats["updated"] += 1
                else:
                    row = GalleryImage(
                        id=image_id,
                        filename=library_filename(source.id, rel),
                        prompt=PurePosixPath(rel).name,
                        model="library",
                        owner=source.owner,
                        source_id=source.id,
                        source_relpath=rel,
                        **fields,
                    )
                    db.add(row)
                    existing[rel] = row
                    stats["new"] += 1
                changed_ids.append(image_id)
            except Exception as exc:  # one unreadable photo must not stop the scan
                stats["errors"] += 1
                if len(error_samples) < _MAX_ERROR_SAMPLES:
                    error_samples.append(f"{rel}: {exc}")
                logger.warning("Library scan: %s failed: %s", rel, exc)
            finally:  # also runs for the `continue`s above (unchanged photos)
                if index % _COMMIT_EVERY == 0:
                    db.commit()
                    progress("scanning", index, total)
        db.commit()
        progress("scanning", min(total, index if images else 0), total)

        if not cancelled:
            # Only mark files missing inside the subfolders this scan covered.
            scanned = {rel for rel, _ in images}
            for rel, row in existing.items():
                if rel in scanned or row.source_state in ("hidden", "missing"):
                    continue
                if prefixes and not any(rel.startswith(p) for p in prefixes):
                    continue
                row.source_state, row.is_active = "missing", False
                stats["missing"] += 1

        stats["cancelled"] = cancelled
        stats["error_samples"] = error_samples
        source.last_scan_at = utcnow_naive()
        source.last_scan_stats = dict(stats)
        db.commit()
        return {"stats": stats, "changed_ids": changed_ids}
    finally:
        db.close()

"""Read-only library sources: manage folders, run indexing, serve originals and thumbnails.

Nothing here writes to a source folder. Removing a source or hiding a photo
only drops Odysseus's own index rows, thumbnails and vectors.
"""

import asyncio
import logging
import os
import uuid
from pathlib import PurePosixPath
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import case, func

from core.database import GalleryAlbum, GalleryImage, GalleryLibrarySource, SessionLocal
from core.middleware import require_admin
from routes.gallery.gallery_helpers import _owner_filter
from src.auth_helpers import get_current_user
from src.gallery_library import embeddings, jobs
from src.gallery_library.albums import sync_albums
from src.gallery_library.files import (
    library_file_path,
    mime_type,
    remove_thumbnail,
    thumb_path,
    write_thumbnail,
)
from src.gallery_library.paths import confine_to_source, display_path, library_mounts, resolve_source_path
from src.gallery_library.scanner import normalize_include_paths
from src.gallery_library.sync import index_task_info, normalize_sync_settings, sync_settings

logger = logging.getLogger(__name__)

FILE_HEADERS = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}


def _source_dict(src: GalleryLibrarySource, counts: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": src.id,
        "name": src.name,
        "path": src.display_path or display_path(src.path),
        "backend_path": src.path,
        "include_paths": src.include_paths or [],
        "enabled": bool(src.enabled),
        "photo_count": counts.get("photos", 0),
        "tagged_count": counts.get("tagged", 0),
        "last_scan_at": src.last_scan_at.isoformat() + "Z" if src.last_scan_at else None,
        "last_scan_stats": src.last_scan_stats or {},
        "sync": sync_settings(src),
        "read_only": True,
    }


def _validate_includes(root: str, raw) -> list:
    includes = normalize_include_paths(raw)
    for include in includes:
        if not os.path.isdir(confine_to_source(root, include)):
            raise ValueError(f"Subfolder not found: {include}")
    return includes


def setup_library_routes() -> APIRouter:
    """Standalone router with the library routes (tests, tooling)."""
    router = APIRouter()
    register_library_routes(router)
    return router


def register_library_routes(router: APIRouter) -> None:
    """Add the library routes to ``router``.

    The gallery router calls this *before* declaring ``/api/gallery/{image_id}``
    so ``/api/gallery/sources`` and ``/api/gallery/folders`` win the match.
    """

    def _owned_source(db, source_id: str, user):
        q = db.query(GalleryLibrarySource).filter(GalleryLibrarySource.id == source_id)
        src = _owner_filter(q, user, GalleryLibrarySource).first()
        if src is None:
            raise HTTPException(404, "Library folder not found")
        return src

    def _counts(db, user) -> Dict[str, Dict[str, int]]:
        q = db.query(
            GalleryImage.source_id,
            func.count(GalleryImage.id),
            func.sum(case(((GalleryImage.ai_tags != None) & (GalleryImage.ai_tags != ""), 1), else_=0)),  # noqa: E711
        ).filter(GalleryImage.source_id != None, GalleryImage.is_active == True)  # noqa: E711,E712
        q = _owner_filter(q, user).group_by(GalleryImage.source_id)
        return {sid: {"photos": int(n or 0), "tagged": int(t or 0)} for sid, n, t in q.all()}

    def _library_image(db, image_id: str, user) -> GalleryImage:
        q = db.query(GalleryImage).filter(
            GalleryImage.id == image_id,
            GalleryImage.source_id != None,  # noqa: E711
            GalleryImage.is_active == True,  # noqa: E712
        )
        img = _owner_filter(q, user).first()
        if img is None:
            raise HTTPException(404, "Image not found")
        return img

    @router.get("/api/gallery/sources")
    async def list_sources(request: Request) -> Dict[str, Any]:
        require_admin(request)
        user = get_current_user(request)
        db = SessionLocal()
        try:
            counts = _counts(db, user)
            q = _owner_filter(db.query(GalleryLibrarySource), user, GalleryLibrarySource)
            sources = [_source_dict(s, counts.get(s.id, {}))
                       for s in q.order_by(GalleryLibrarySource.created_at).all()]
            return {
                "sources": sources,
                "mounts": [m.host for m in library_mounts()],
                "job": jobs.status(),
                # New photos are picked up by the scheduled "Gallery Library Index" task.
                "index_task": index_task_info(user),
            }
        finally:
            db.close()

    @router.post("/api/gallery/sources")
    async def add_source(request: Request) -> Dict[str, Any]:
        require_admin(request)
        user = get_current_user(request)
        body = await request.json()
        typed = str(body.get("path") or "").strip()
        try:
            backend = resolve_source_path(typed)
            includes = _validate_includes(backend, body.get("include_paths") or [])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db = SessionLocal()
        try:
            dup = _owner_filter(
                db.query(GalleryLibrarySource).filter(GalleryLibrarySource.path == backend),
                user, GalleryLibrarySource,
            ).first()
            if dup is not None:
                raise HTTPException(409, f"This folder is already in your library as '{dup.name}'")
            name = str(body.get("name") or "").strip()[:120] or PurePosixPath(
                typed.replace("\\", "/").rstrip("/")).name or "Library"
            src = GalleryLibrarySource(
                id=str(uuid.uuid4()), owner=user, name=name, path=backend,
                display_path=typed, include_paths=includes, enabled=True,
            )
            db.add(src)
            db.commit()
            return {"ok": True, "source": _source_dict(src, {})}
        finally:
            db.close()

    @router.patch("/api/gallery/sources/{source_id}")
    async def update_source(request: Request, source_id: str) -> Dict[str, Any]:
        require_admin(request)
        user = get_current_user(request)
        body = await request.json()
        db = SessionLocal()
        try:
            src = _owned_source(db, source_id, user)
            if "name" in body:
                src.name = str(body.get("name") or "").strip()[:120] or src.name
            if "include_paths" in body:
                try:
                    src.include_paths = _validate_includes(src.path, body.get("include_paths") or [])
                except ValueError as exc:
                    raise HTTPException(400, str(exc))
            if "enabled" in body:
                src.enabled = bool(body.get("enabled"))
            albums_turned_on = False
            if "sync" in body:
                before = sync_settings(src)
                src.sync_settings = normalize_sync_settings(body.get("sync"), base=before)
                albums_turned_on = src.sync_settings["albums"] and not before["albums"]
                if albums_turned_on:
                    src.album_skip = []   # asking for albums again brings deleted ones back
            db.commit()
            if albums_turned_on:
                await asyncio.to_thread(sync_albums, src.id)
            return {"ok": True, "source": _source_dict(src, _counts(db, user).get(src.id, {}))}
        finally:
            db.close()

    @router.delete("/api/gallery/sources/{source_id}")
    async def remove_source(request: Request, source_id: str) -> Dict[str, Any]:
        """Forget a folder: drops index rows, thumbnails and vectors. The folder itself is untouched."""
        require_admin(request)
        user = get_current_user(request)
        if jobs.is_running(source_id):
            raise HTTPException(409, "Cancel the running scan first")
        db = SessionLocal()
        try:
            src = _owned_source(db, source_id, user)
            ids = [i for (i,) in db.query(GalleryImage.id).filter(GalleryImage.source_id == src.id)]
            embeddings.delete_ids(ids)
            for image_id in ids:
                remove_thumbnail(image_id)
            from src.gallery_faces.index import delete_faces_of_images
            delete_faces_of_images(db, ids)
            db.query(GalleryImage).filter(GalleryImage.source_id == src.id).delete(synchronize_session=False)
            db.query(GalleryAlbum).filter(GalleryAlbum.source_id == src.id).delete(synchronize_session=False)
            db.delete(src)
            db.commit()
            return {"ok": True, "removed_photos": len(ids)}
        finally:
            db.close()

    @router.post("/api/gallery/sources/{source_id}/scan")
    async def scan_source(request: Request, source_id: str) -> Dict[str, Any]:
        require_admin(request)
        user = get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        db = SessionLocal()
        try:
            src = _owned_source(db, source_id, user)
            if not src.enabled:
                raise HTTPException(400, "This library folder is disabled")
            # The Scan choices become the folder's auto-sync choices.
            settings = normalize_sync_settings(
                {k: body[k] for k in ("embed", "tag", "faces", "albums") if k in body}, base=sync_settings(src))
            src.sync_settings = settings
            db.commit()
        finally:
            db.close()
        try:
            job = jobs.start(source_id, embed=settings["embed"], tag=settings["tag"], faces=settings["faces"])
        except jobs.JobAlreadyRunning as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True, "job": job}

    @router.get("/api/gallery/sources/job")
    async def job_status(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return {"job": jobs.status()}

    @router.post("/api/gallery/sources/job/cancel")
    async def cancel_job(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return {"ok": jobs.cancel()}

    @router.post("/api/gallery/smart-search/warmup")
    async def smart_search_warmup(request: Request) -> Dict[str, Any]:
        """Start loading the smart-search query model so the first search is fast."""
        get_current_user(request)
        return {"ready": embeddings.warm_up()}

    @router.get("/api/gallery/folders")
    async def list_folders(request: Request) -> Dict[str, Any]:
        """Folder tree (with photo counts, including subfolders) for the folder filter."""
        user = get_current_user(request)
        db = SessionLocal()
        try:
            names = {s.id: s.name for s in _owner_filter(
                db.query(GalleryLibrarySource), user, GalleryLibrarySource).all()}
            q = db.query(GalleryImage.source_id, GalleryImage.source_relpath).filter(
                GalleryImage.source_id != None, GalleryImage.is_active == True)  # noqa: E711,E712
            counts: Dict[tuple, int] = {}
            for source_id, rel in _owner_filter(q, user).all():
                if source_id not in names or not rel:
                    continue
                parts = PurePosixPath(rel).parts[:-1]
                for depth in range(len(parts) + 1):
                    key = (source_id, "/".join(parts[:depth]))
                    counts[key] = counts.get(key, 0) + 1
            folders = [
                {"source_id": sid, "source_name": names[sid], "path": path, "count": n}
                for (sid, path), n in sorted(counts.items(), key=lambda kv: (names[kv[0][0]].lower(), kv[0][1].lower()))
            ]
            return {"folders": folders}
        finally:
            db.close()

    @router.get("/api/gallery/{image_id}/file")
    async def library_original(request: Request, image_id: str):
        """Stream a library original straight from the (read-only) source folder."""
        user = get_current_user(request)
        db = SessionLocal()
        try:
            img = _library_image(db, image_id, user)
            try:
                path = library_file_path(img, db)
            except FileNotFoundError:
                raise HTTPException(404, "Image not found")
        finally:
            db.close()
        if not os.path.isfile(path):
            raise HTTPException(404, "Image file not reachable")
        return FileResponse(path, media_type=mime_type(path), headers=FILE_HEADERS)

    @router.get("/api/gallery/{image_id}/thumb")
    async def library_thumbnail(request: Request, image_id: str):
        user = get_current_user(request)
        db = SessionLocal()
        try:
            img = _library_image(db, image_id, user)
            thumb = thumb_path(img.id)
            if not os.path.isfile(thumb):  # e.g. after a data wipe: rebuild from the original
                try:
                    with open(library_file_path(img, db), "rb") as fh:
                        write_thumbnail(img.id, fh.read())
                except (FileNotFoundError, OSError, ValueError):
                    raise HTTPException(404, "Thumbnail not available")
        finally:
            db.close()
        return FileResponse(thumb, media_type="image/jpeg", headers=FILE_HEADERS)


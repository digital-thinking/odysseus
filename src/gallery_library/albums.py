"""Automatic albums: one album per top-level folder of a library source.

Only Odysseus's own index changes; the source folder is never touched. An album
keeps its name once made, so renaming it is safe. Photos the user filed in
another album stay there. A deleted album is remembered and not made again.
"""

from __future__ import annotations

import re
import uuid

_YEAR_PREFIX = re.compile(r"^(\d{4})[ _-]+(.+)$")
_WORD_BOUNDARY = re.compile(r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ])")
_UPDATE_BATCH = 500


def album_name(folder: str) -> str:
    """``2016_BadGastein`` -> ``Bad Gastein 2016``; other names only lose their underscores."""
    match = _YEAR_PREFIX.match(folder)
    year, rest = (match.group(1), match.group(2)) if match else ("", folder)
    words = " ".join(_WORD_BOUNDARY.sub(" ", rest.replace("_", " ")).split())
    return f"{words} {year}".strip() or folder


def sync_albums(source_id: str) -> dict:
    """Bring the source's automatic albums in line with its folders."""
    from core.database import GalleryAlbum, GalleryImage, GalleryLibrarySource, SessionLocal

    db = SessionLocal()
    try:
        source = db.query(GalleryLibrarySource).filter(GalleryLibrarySource.id == source_id).first()
        if source is None:
            return {"created": 0, "added": 0, "removed": 0}
        skip = set(source.album_skip or [])
        folders: dict[str, list] = {}
        rows = db.query(GalleryImage.id, GalleryImage.source_relpath, GalleryImage.album_id).filter(
            GalleryImage.source_id == source_id, GalleryImage.is_active == True)  # noqa: E712
        for image_id, relpath, album_id in rows:
            folder, slash, _rest = (relpath or "").partition("/")
            if slash:   # photos directly in the source root belong to no trip
                folders.setdefault(folder, []).append((image_id, album_id))

        albums = {a.source_folder: a for a in db.query(GalleryAlbum).filter(GalleryAlbum.source_id == source_id)}
        created = added = removed = 0
        for folder, photos in sorted(folders.items()):
            if folder in skip:
                continue
            album = albums.get(folder)
            if album is None:
                album = GalleryAlbum(id=str(uuid.uuid4()), name=album_name(folder), owner=source.owner,
                                     source_id=source_id, source_folder=folder)
                db.add(album)
                db.flush()
                created += 1
            unfiled = [image_id for image_id, album_id in photos if album_id is None]
            for start in range(0, len(unfiled), _UPDATE_BATCH):
                batch = unfiled[start:start + _UPDATE_BATCH]
                db.query(GalleryImage).filter(GalleryImage.id.in_(batch)).update(
                    {GalleryImage.album_id: album.id}, synchronize_session=False)
            added += len(unfiled)

        # A folder that was renamed or emptied leaves an album with nothing in it.
        for folder, album in albums.items():
            if folder not in folders:
                db.query(GalleryImage).filter(GalleryImage.album_id == album.id).update(
                    {GalleryImage.album_id: None}, synchronize_session=False)
                db.delete(album)
                removed += 1
        db.commit()
        return {"created": created, "added": added, "removed": removed}
    finally:
        db.close()

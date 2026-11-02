"""File access for library images: originals (read-only) and derived thumbnails."""

from __future__ import annotations

import hashlib
import os
import re
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Optional

from src.constants import DATA_DIR
from src.gallery_library.paths import confine_to_source, is_inside_library

LIBRARY_DATA_DIR = os.path.join(DATA_DIR, "gallery_library")
THUMB_DIR = os.path.join(LIBRARY_DATA_DIR, "thumbs")
THUMB_SIZE = 512

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
VIDEO_EXTS = {".3gp", ".mp4", ".mov", ".avi", ".m4v", ".mkv", ".webm", ".mpg", ".mpeg", ".vob", ".wmv"}
MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class ReadOnlyLibraryError(RuntimeError):
    """Raised when something tries to write inside a read-only library source."""


def is_library_image(img) -> bool:
    return bool(getattr(img, "source_id", None))


def library_filename(source_id: str, relpath: str) -> str:
    """Synthetic unique ``GalleryImage.filename`` for a library row.

    Never names a real file in generated_images and never matches the
    generated-image filename pattern, so the legacy file routes cannot serve or
    delete anything for library rows.
    """
    digest = hashlib.sha1(f"{source_id}/{relpath}".encode("utf-8")).hexdigest()[:24]
    ext = PurePosixPath(relpath).suffix.lower().lstrip(".") or "jpg"
    return f"lib-{digest}.{ext}"


def mime_type(path: str) -> str:
    return MIME_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def source_file_path(source_root: str, relpath: str) -> str:
    """Absolute, confined path of a library original (read-only use only)."""
    return confine_to_source(source_root, relpath)


def library_file_path(img, db) -> Optional[str]:
    """Original file of a library image, or None for regular gallery images.

    :raises FileNotFoundError: the source row is gone or the path escapes it.
    """
    if not is_library_image(img):
        return None
    from core.database import GalleryLibrarySource
    source = db.query(GalleryLibrarySource).filter(GalleryLibrarySource.id == img.source_id).first()
    if source is None or not img.source_relpath:
        raise FileNotFoundError("library source not found")
    try:
        return source_file_path(source.path, img.source_relpath)
    except ValueError as exc:
        raise FileNotFoundError(str(exc)) from exc


def ensure_writable_derived_path(path: str) -> str:
    """Refuse any derived-file write that would land inside a library source."""
    if is_inside_library(path):
        raise ReadOnlyLibraryError(f"refusing to write inside a read-only library folder: {path}")
    return path


def thumb_path(image_id: str) -> str:
    if not _SAFE_ID.match(str(image_id or "")):
        raise ValueError("invalid image id")
    return os.path.join(THUMB_DIR, f"{image_id}.jpg")


def write_thumbnail(image_id: str, content: bytes) -> tuple[int, int]:
    """Render a JPEG thumbnail (longest side THUMB_SIZE) of ``content`` into Odysseus's data dir.

    Returns the original image size (width, height) after EXIF orientation.
    """
    from PIL import Image, ImageOps

    dest = ensure_writable_derived_path(thumb_path(image_id))
    with Image.open(BytesIO(content)) as im:
        width, height = im.size  # header size, before draft() shrinks the decode
        try:
            if im.getexif().get(0x0112) in (5, 6, 7, 8):  # orientations that swap axes
                width, height = height, width
        except Exception:
            pass
        if im.format == "JPEG":
            im.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))  # fast DCT downscale
        oriented = ImageOps.exif_transpose(im)
        rgb = oriented.convert("RGB")
        rgb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        os.makedirs(THUMB_DIR, exist_ok=True)
        tmp = dest + ".tmp"
        rgb.save(tmp, "JPEG", quality=85, optimize=True)
        os.replace(tmp, dest)
    return int(width), int(height)


def remove_thumbnail(image_id: str) -> None:
    try:
        os.remove(thumb_path(image_id))
    except (OSError, ValueError):
        pass

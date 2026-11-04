"""How many photos carry each tag, kept in memory until the photos change.

Counting means reading every photo's tag text, about a second on a large
library, so the result is reused for as long as a quick check shows no photo
was added, removed or edited.
"""

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import load_only

from core.database import GalleryImage
from routes.gallery.gallery_helpers import _image_urls, _owner_filter

# Tags compare trimmed and with ASCII letters lower-cased: exactly what SQLite's
# lower()/LIKE do, so the Tags tab counts and the exact_tag filter always agree.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def tag_key(tag: str) -> str:
    return str(tag or "").strip().translate(_ASCII_LOWER)


def tag_keys(tags: Optional[str], ai_tags: Optional[str]) -> set:
    """The distinct tags of one photo: the user's own and the AI tags."""
    return {tag_key(t) for t in f"{tags or ''},{ai_tags or ''}".split(",")} - {""}


@dataclass(frozen=True)
class TagStats:
    counts: Dict[str, int]
    covers: Dict[str, str]    # tag -> thumbnail of its newest photo
    ranked: List[str]         # most used first, ties in alphabetical order


_lock = threading.Lock()
_cache: Dict[Tuple[int, Optional[str]], Tuple[tuple, TagStats]] = {}


def _fingerprint(db, user) -> tuple:
    """Changes whenever a photo is added, removed or edited (hiding one edits it)."""
    rows = _owner_filter(db.query(GalleryImage), user)
    return (rows.with_entities(func.count(GalleryImage.id)).scalar(),
            rows.with_entities(func.max(GalleryImage.updated_at)).scalar())


def _build(db, user) -> TagStats:
    rows = (_owner_filter(db.query(GalleryImage), user)
            .options(load_only(GalleryImage.id, GalleryImage.filename, GalleryImage.source_id,
                               GalleryImage.tags, GalleryImage.ai_tags))
            .filter(GalleryImage.is_active == True)  # noqa: E712
            .order_by(func.coalesce(GalleryImage.taken_at, GalleryImage.created_at).desc(), GalleryImage.id)
            .all())
    counts: Dict[str, int] = {}
    covers: Dict[str, str] = {}
    for img in rows:
        for tag in tag_keys(img.tags, img.ai_tags):
            counts[tag] = counts.get(tag, 0) + 1
            if tag not in covers:
                covers[tag] = _image_urls(img)[1]
    return TagStats(counts, covers, sorted(counts, key=lambda t: (-counts[t], t)))


def current(session_factory, user) -> TagStats:
    """The user's tag statistics. Blocks for the rebuild, so call it off the event loop."""
    key = (id(session_factory), user)
    db = session_factory()
    try:
        with _lock:
            fingerprint = _fingerprint(db, user)
            cached = _cache.get(key)
            if cached and cached[0] == fingerprint:
                return cached[1]
            stats = _build(db, user)
            _cache[key] = (fingerprint, stats)
            return stats
    finally:
        db.close()

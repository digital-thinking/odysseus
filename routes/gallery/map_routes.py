"""Photo locations: the gallery map and searching for a place.

A photo's position comes from its EXIF GPS data (``gps_lat`` / ``gps_lng``,
stored as text). The map gets every located photo as one compact list and
groups them into bubbles in the browser. Listing photos by distance from a
place is the ``near`` option of ``/api/gallery/library``, using
``rank_by_distance`` from here.

Place names are looked up with OpenStreetMap's Nominatim, through the server
so the browser only talks to Odysseus. Its usage policy allows one request a
second from an application that names itself, so lookups are spaced out and
remembered. Typed coordinates ("49.02, 8.35") need no lookup at all.
"""

import asyncio
import logging
import math
import re
import time
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func

from core.database import GalleryImage, SessionLocal
from routes.gallery.gallery_helpers import _owner_filter
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0088
# Map positions are rounded to about 10 m: closer photos share one entry.
_MAP_DECIMALS = 4

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "Odysseus photo gallery (self-hosted; +https://github.com/odysseus-dev/odysseus)"
_NOMINATIM_INTERVAL = 1.0      # seconds between two lookups (usage policy)
_PLACE_RESULTS = 6
_PLACE_QUERY_MAX = 160
_PLACE_CACHE_SIZE = 256

_COORDINATES = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_LANGUAGE = re.compile(r"[^A-Za-z0-9,;=.\- *]")


# ---------------------------------------------------------------- geometry

def to_point(lat: Any, lng: Any) -> Optional[Tuple[float, float]]:
    """(lat, lng) as numbers, or None if either is missing or not on Earth."""
    try:
        la, ln = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(la) and math.isfinite(ln)) or abs(la) > 90 or abs(ln) > 180:
        return None
    return la, ln


def parse_point(raw: Optional[str]) -> Optional[Tuple[float, float]]:
    """"lat,lng" from a query string, or None."""
    parts = (raw or "").split(",")
    return to_point(*parts) if len(parts) == 2 else None


def distance_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance (haversine)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def rank_by_distance(rows: Iterable[Tuple[str, Any, Any, Optional[datetime]]],
                     origin: Tuple[float, float]) -> List[Tuple[str, float]]:
    """(id, km) of each (id, lat, lng, taken_at) row, nearest first.

    Photos at the same spot (to the metre) keep the order they were taken in.
    Rows without a usable position are left out.
    """
    ranked = []
    for image_id, lat, lng, taken_at in rows:
        point = to_point(lat, lng)
        if point is None:
            continue
        km = distance_km(origin, point)
        ranked.append(((round(km, 3), taken_at is None, taken_at or datetime.min, image_id), km))
    ranked.sort(key=lambda r: r[0])
    return [(key[3], km) for key, km in ranked]


# ---------------------------------------------------------------- place search

_place_cache: "OrderedDict[Tuple[str, str], List[Dict[str, Any]]]" = OrderedDict()
_nominatim_lock = asyncio.Lock()
_nominatim_last = 0.0


class PlaceSearchUnavailable(Exception):
    pass


def _coordinates_place(text: str) -> Optional[Dict[str, Any]]:
    match = _COORDINATES.match(text)
    point = to_point(match.group(1), match.group(2)) if match else None
    if point is None:
        return None
    label = f"{point[0]:.5f}, {point[1]:.5f}"
    return {"name": label, "label": "Coordinates", "lat": point[0], "lng": point[1], "kind": "coordinates", "bbox": None}


def _place_from_nominatim(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    point = to_point(item.get("lat"), item.get("lon"))
    if point is None:
        return None
    label = str(item.get("display_name") or "").strip()
    name = str(item.get("name") or "").strip() or label.split(",")[0].strip() or f"{point[0]:.4f}, {point[1]:.4f}"
    bbox = None
    box = item.get("boundingbox")
    if isinstance(box, list) and len(box) == 4:
        south_west, north_east = to_point(box[0], box[2]), to_point(box[1], box[3])
        if south_west and north_east:
            bbox = [south_west[0], north_east[0], south_west[1], north_east[1]]   # south, north, west, east
    return {"name": name, "label": label, "lat": point[0], "lng": point[1],
            "kind": item.get("addresstype") or item.get("type") or "", "bbox": bbox}


async def _nominatim_search(text: str, language: str) -> List[Dict[str, Any]]:
    """Raw Nominatim results, at most one request a second for the whole app."""
    global _nominatim_last
    headers = {"User-Agent": NOMINATIM_USER_AGENT}
    if language:
        headers["Accept-Language"] = language
    params = {"q": text, "format": "jsonv2", "limit": _PLACE_RESULTS}
    async with _nominatim_lock:
        wait = _nominatim_last + _NOMINATIM_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
                resp = await client.get(NOMINATIM_URL, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise PlaceSearchUnavailable(str(exc) or type(exc).__name__) from exc
        finally:
            _nominatim_last = time.monotonic()
    if resp.status_code != 200:
        raise PlaceSearchUnavailable(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise PlaceSearchUnavailable("unreadable answer") from exc
    return data if isinstance(data, list) else []


async def search_places(text: str, language: str = "") -> List[Dict[str, Any]]:
    place = _coordinates_place(text)
    if place:
        return [place]
    key = (" ".join(text.lower().split()), language)
    if key in _place_cache:
        _place_cache.move_to_end(key)
        return _place_cache[key]
    places = [p for p in map(_place_from_nominatim, await _nominatim_search(text, language)) if p]
    _place_cache[key] = places
    while len(_place_cache) > _PLACE_CACHE_SIZE:
        _place_cache.popitem(last=False)
    return places


# ---------------------------------------------------------------- routes

def register_map_routes(router: APIRouter) -> None:
    """Add the map routes (before the /api/gallery/{image_id} catch-all)."""

    @router.get("/api/gallery/map")
    async def gallery_map(request: Request) -> Dict[str, Any]:
        """Where the photos were taken: [lat, lng, photos] per spot (about 10 m)."""
        user = get_current_user(request)

        def load():
            db = SessionLocal()
            try:
                located = db.query(GalleryImage.gps_lat, GalleryImage.gps_lng).filter(
                    GalleryImage.is_active == True, GalleryImage.gps_lat.isnot(None))  # noqa: E712
                rows = _owner_filter(located, user).all()
                photos = _owner_filter(db.query(func.count(GalleryImage.id))
                                       .filter(GalleryImage.is_active == True), user).scalar()  # noqa: E712
                return rows, photos or 0
            finally:
                db.close()

        rows, photos = await asyncio.to_thread(load)
        spots = Counter()
        for lat, lng in rows:
            point = to_point(lat, lng)
            if point is not None:
                spots[(round(point[0], _MAP_DECIMALS), round(point[1], _MAP_DECIMALS))] += 1
        return {
            "points": [[lat, lng, n] for (lat, lng), n in spots.items()],
            "located": sum(spots.values()),
            "photos": photos,
        }

    @router.get("/api/gallery/places")
    async def gallery_places(request: Request, q: str = Query("", max_length=_PLACE_QUERY_MAX)) -> Dict[str, Any]:
        """Places matching a name or address, or typed coordinates."""
        text = " ".join(q.split())
        if not text:
            raise HTTPException(400, "Enter a place to search for")
        language = _LANGUAGE.sub("", request.headers.get("accept-language", ""))[:100]
        try:
            places = await search_places(text, language)
        except PlaceSearchUnavailable as exc:
            logger.warning("Place search failed: %s", exc)
            raise HTTPException(502, "Place search is not reachable right now. "
                                     "You can still click the map to pick a spot.")
        return {"places": places, "source": "OpenStreetMap Nominatim"}

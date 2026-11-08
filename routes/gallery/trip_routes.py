"""Trips and "On this day": the gallery by time and place.

Trips are found in the photos' locations (src/gallery_library/trips.py) and
named offline (src/gallery_library/places.py). Before phones recorded where a
photo was taken, a folder album that spans a few weeks at most stands in for a
trip. Both are worked out again only after the library changed.

"On this day" lists photos taken on today's date in earlier years. Opening a
trip or a day in Photos is the ``since`` / ``until`` filter of
/api/gallery/library.
"""

import asyncio
import bisect
import logging
import re
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func

from core.database import GalleryAlbum, GalleryImage, SessionLocal
from routes.gallery.gallery_helpers import NEWEST_KEY, TAKEN_KEY, _image_to_dict, _image_urls, _owner_filter
from routes.gallery.map_routes import to_point
from src.auth_helpers import get_current_user
from src.gallery_library import places
from src.gallery_library.trips import find_trips

logger = logging.getLogger(__name__)

MIN_TRIP_PHOTOS = 8
REGULAR_VISITS = 4              # a place you go to this often is a visit (family), not a trip
ALBUM_TRIP_MAX_DAYS = 31        # a folder album longer than this is not one trip
_COVERS = 4
_PLACES_SHOWN = 4
_DAY_PHOTOS = 8                 # photos shown per year in "On this day"
_YEAR_ONLY = re.compile(r"^\s*\d{4}\s*$")

_lock = threading.Lock()
_trip_cache: Dict[Any, tuple] = {}      # owner -> (library signature, trips)
_day_cache: Dict[Any, tuple] = {}       # owner -> (signature, date, result)


def library_signature(db, user) -> tuple:
    """Changes whenever photos or albums are added, removed or edited (two index-only reads)."""
    photos = _owner_filter(db.query(func.count(), func.max(GalleryImage.updated_at)), user).one()
    albums = _owner_filter(db.query(func.count(), func.max(GalleryAlbum.updated_at)), user, GalleryAlbum).one()
    return tuple(photos) + tuple(albums)


@lru_cache(maxsize=20000)
def _town(lat: float, lng: float) -> Optional[dict]:
    return places.nearest_town(lat, lng)


def _town_of(lat: float, lng: float) -> Optional[dict]:
    return _town(round(lat, 2), round(lng, 2))   # about 1 km: nearby photos share one lookup


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _spread(items: list, n: int) -> list:
    """n items evenly spread over the list (first and last included)."""
    if len(items) <= n:
        return list(items)
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def _in_range(q, start: datetime, end: datetime):
    return q.filter(TAKEN_KEY >= start, TAKEN_KEY < end)


def _located_trips(db, user, located: list, albums: dict) -> list:
    trips = []
    for trip in find_trips(located):
        active = _owner_filter(db.query(GalleryImage.album_id, func.count())
                               .filter(GalleryImage.is_active == True), user)  # noqa: E712
        per_album = dict(_in_range(active, trip.start, trip.end).group_by(GalleryImage.album_id).all())
        photos = sum(per_album.values())
        if photos < MIN_TRIP_PHOTOS:
            continue
        album = None
        named = [(n, a) for a, n in per_album.items() if a in albums and not _YEAR_ONLY.match(albums[a].name)]
        if named:
            count, album_id = max(named)
            if count * 2 >= photos:
                album = {"id": album_id, "name": albums[album_id].name}

        home_town = _town_of(*trip.home)
        towns: Counter = Counter()
        first_seen: Dict[str, datetime] = {}
        countries: Counter = Counter()
        for _id, when, lat, lng in trip.points:
            town = _town_of(lat, lng)
            if town is None:
                continue
            towns[town["name"]] += 1
            first_seen.setdefault(town["name"], when)
            countries[town["country"]] += 1
        shown = sorted((name for name, _n in towns.most_common(_PLACES_SHOWN)), key=first_seen.get)
        main_place = towns.most_common(1)[0][0] if towns else None
        lats = [p[2] for p in trip.points]
        lngs = [p[3] for p in trip.points]
        trips.append({
            "kind": "located",
            "start": _iso(trip.start),
            "end": _iso(trip.end),
            "days": trip.days,
            "photos": photos,
            "located": len(trip.points),
            "far_km": round(trip.far_km),
            "album": album,
            "places": shown,
            "main_place": main_place,
            "countries": [c for c, _n in countries.most_common(3)],
            "home_country": home_town["country"] if home_town else None,
            "bbox": [min(lats), max(lats), min(lngs), max(lngs)],
            "cover_ids": [p[0] for p in _spread(trip.points, _COVERS)],
        })
    return trips


def _album_trips(db, user, albums: dict, located_times: list, taken: list) -> list:
    """Folder albums from before photos had a location that span a few weeks at most."""
    trips = []
    for album in albums.values():
        if album.source_id is None or _YEAR_ONLY.match(album.name):
            continue
        in_album = _owner_filter(db.query(GalleryImage).filter(
            GalleryImage.album_id == album.id, GalleryImage.is_active == True), user)  # noqa: E712
        photos = in_album.with_entities(func.count()).scalar()
        if photos < MIN_TRIP_PHOTOS:
            continue
        # Library photos sort by the date taken; ix_gallery_images_album_newest has it in order.
        first = in_album.with_entities(NEWEST_KEY).order_by(NEWEST_KEY.asc()).limit(1).scalar()
        last = in_album.with_entities(NEWEST_KEY).order_by(NEWEST_KEY.desc()).limit(1).scalar()
        if first is None or last is None or last - first > timedelta(days=ALBUM_TRIP_MAX_DAYS):
            continue
        end = last + timedelta(microseconds=1)
        # Located photos in that time: the trips found from locations cover it.
        i = bisect.bisect_left(located_times, first)
        if i < len(located_times) and located_times[i] < end:
            continue
        if any(start < end and first < stop for start, stop in taken):
            continue
        covers = [i for (i,) in in_album.with_entities(GalleryImage.id)
                  .order_by(NEWEST_KEY.asc()).limit(_COVERS * 8).all()]
        covers = _spread(covers, _COVERS)
        if album.cover_id and album.cover_id not in covers:
            covers = [album.cover_id] + covers[:_COVERS - 1]
        trips.append({
            "kind": "album",
            "start": _iso(first),
            "end": _iso(end),
            "days": (last.date() - first.date()).days + 1,
            "photos": photos,
            "located": 0,
            "far_km": None,
            "album": {"id": album.id, "name": album.name},
            "places": [],
            "main_place": None,
            "countries": [],
            "home_country": None,
            "bbox": None,
            "cover_ids": covers,
        })
    return trips


def compute_trips(db, user) -> List[Dict[str, Any]]:
    rows = _owner_filter(db.query(GalleryImage.id, GalleryImage.taken_at, GalleryImage.gps_lat,
                                  GalleryImage.gps_lng)
                         .filter(GalleryImage.is_active == True, GalleryImage.gps_lat.isnot(None)),  # noqa: E712
                         user).all()
    located = []
    for image_id, taken_at, lat, lng in rows:
        point = to_point(lat, lng)
        if point is not None and taken_at is not None:
            located.append((image_id, taken_at, point[0], point[1]))
    albums = {a.id: a for a in _owner_filter(db.query(GalleryAlbum), user, GalleryAlbum)}

    trips = _located_trips(db, user, located, albums)
    visits = Counter(t["main_place"] for t in trips if t["album"] is None and t["main_place"])
    for t in trips:
        t["regular"] = t["album"] is None and visits[t["main_place"]] >= REGULAR_VISITS
    taken = [(datetime.fromisoformat(t["start"]), datetime.fromisoformat(t["end"])) for t in trips]
    for t in _album_trips(db, user, albums, sorted(p[1] for p in located), taken):
        t["regular"] = False
        trips.append(t)

    # Cover thumbnails: one read for all trips.
    wanted = {i for t in trips for i in t["cover_ids"]}
    found = {}
    for chunk in (list(wanted)[i:i + 500] for i in range(0, len(wanted), 500)):
        for row in db.query(GalleryImage.id, GalleryImage.source_id, GalleryImage.filename).filter(
                GalleryImage.id.in_(chunk)):
            found[row.id] = _image_urls(row)[1]
    for t in trips:
        t["covers"] = [found[i] for i in t.pop("cover_ids") if i in found]
    trips.sort(key=lambda t: t["start"], reverse=True)
    return trips


def _cached_trips(user) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        signature = library_signature(db, user)
        with _lock:
            cached = _trip_cache.get(user)
            if cached and cached[0] == signature:
                return cached[1]
            result = {"trips": compute_trips(db, user)}
            _trip_cache[user] = (signature, result)
            return result
    finally:
        db.close()


def photos_on_this_day(db, user, day: date) -> List[Dict[str, Any]]:
    """Earlier years' photos taken on this month and day, newest year first."""
    month_day = f"{day.month:02d}-{day.day:02d}"
    rows = _owner_filter(db.query(GalleryImage.id, TAKEN_KEY).filter(
        GalleryImage.is_active == True,  # noqa: E712
        func.substr(TAKEN_KEY, 6, 5) == month_day,
        TAKEN_KEY < datetime(day.year, 1, 1)), user).all()
    by_year: Dict[int, list] = defaultdict(list)
    for image_id, taken in rows:
        if isinstance(taken, str):    # SQLite hands the expression back as text
            taken = datetime.fromisoformat(taken)
        by_year[taken.year].append((taken, image_id))
    shown = {year: [i for _t, i in _spread(sorted(items), _DAY_PHOTOS)] for year, items in by_year.items()}
    wanted = [i for ids in shown.values() for i in ids]
    found = {img.id: img for img in db.query(GalleryImage).filter(GalleryImage.id.in_(wanted or [""]))}
    return [{
        "year": year,
        "years_ago": day.year - year,
        "count": len(by_year[year]),
        "items": [_image_to_dict(found[i]) for i in shown[year] if i in found],
    } for year in sorted(by_year, reverse=True)]


def _cached_day(user, day: date) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        signature = library_signature(db, user)
        with _lock:
            cached = _day_cache.get(user)
            if cached and cached[0] == signature and cached[1] == day:
                return cached[2]
        years = photos_on_this_day(db, user, day)
        with _lock:
            _day_cache[user] = (signature, day, years)
        return years
    finally:
        db.close()


def register_trip_routes(router: APIRouter) -> None:
    """Add the trip routes (before the /api/gallery/{image_id} catch-all)."""

    @router.get("/api/gallery/trips")
    async def gallery_trips(request: Request) -> Dict[str, Any]:
        user = get_current_user(request)
        try:
            return await asyncio.to_thread(_cached_trips, user)
        except Exception:
            logger.exception("Failed to work out trips")
            raise HTTPException(500, "Failed to work out trips")

    @router.get("/api/gallery/on-this-day")
    async def gallery_on_this_day(request: Request, day: str = Query(..., alias="date")) -> Dict[str, Any]:
        """Photos from earlier years taken on this date (the browser's today)."""
        try:
            when = date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")
        user = get_current_user(request)
        years = await asyncio.to_thread(_cached_day, user, when)
        return {"date": when.isoformat(), "years": years}

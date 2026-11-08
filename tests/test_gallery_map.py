"""Gallery map: where photos were taken, nearest-first listing, and place search."""
import asyncio
import re
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import Base, GalleryImage
from routes.gallery import map_routes

KARLSRUHE = (49.0069, 8.4037)
MUNICH = (48.1374, 11.5755)
ROME = (41.9028, 12.4964)


@pytest.fixture
def gallery(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import routes.gallery.gallery_routes as gallery_routes
    for module in (gallery_routes, map_routes):
        monkeypatch.setattr(module, "SessionLocal", session_local)
        monkeypatch.setattr(module, "get_current_user", lambda request: request.headers.get("x-user", "alice"))
    monkeypatch.setattr(map_routes, "_place_cache", type(map_routes._place_cache)())

    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    return {"db": session_local, "client": TestClient(app), "engine": engine}


def _photo(gallery, image_id, at=None, **fields):
    db = gallery["db"]()
    lat, lng = at if at else (None, None)
    db.add(GalleryImage(id=image_id, filename=f"{image_id}.jpg", prompt=fields.pop("prompt", image_id),
                        owner=fields.pop("owner", "alice"), is_active=fields.pop("is_active", True),
                        gps_lat=None if lat is None else f"{lat:.6f}", gps_lng=None if lng is None else f"{lng:.6f}",
                        **fields))
    db.commit()
    db.close()


def _near(gallery, point, **params):
    resp = gallery["client"].get("/api/gallery/library", params={"near": f"{point[0]},{point[1]}", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- geometry

def test_distances_are_great_circle_kilometres():
    assert map_routes.distance_km((0, 0), (0, 1)) == pytest.approx(111.195, abs=0.01)
    assert map_routes.distance_km(KARLSRUHE, MUNICH) == pytest.approx(254, abs=3)
    # across the date line it is the short way round
    assert map_routes.distance_km((0, 179.5), (0, -179.5)) == pytest.approx(111.195, abs=0.01)


def test_positions_that_are_not_on_earth_are_ignored():
    assert map_routes.parse_point("49.0069,8.4037") == KARLSRUHE
    for bad in ("", "49.0", "a,b", "91,0", "0,181", "nan,1", "inf,1", "1,2,3"):
        assert map_routes.parse_point(bad) is None, bad
    rows = [("ok", "1", "1", None), ("text", "north", "1", None), ("empty", None, None, None)]
    assert [i for i, _ in map_routes.rank_by_distance(rows, (0, 0))] == ["ok"]


# ---------------------------------------------------------------- nearest first

def test_nearest_first_lists_only_located_photos_with_their_distance(gallery):
    _photo(gallery, "rome", ROME)
    _photo(gallery, "munich", MUNICH, ai_tags="beer garden")
    _photo(gallery, "karlsruhe", KARLSRUHE)
    _photo(gallery, "no-gps", ai_tags="beach")
    _photo(gallery, "deleted", KARLSRUHE, is_active=False)
    _photo(gallery, "bobs", KARLSRUHE, owner="bob", ai_tags="castle")

    data = _near(gallery, (49.0, 8.4))
    assert [i["id"] for i in data["items"]] == ["karlsruhe", "munich", "rome"]
    assert (data["total"], data["total_tagged"]) == (3, 1)
    km = [i["distance_km"] for i in data["items"]]
    assert km[0] < 1 and km[1] == pytest.approx(254, abs=3) and km[2] > km[1]

    page = _near(gallery, (49.0, 8.4), offset=1, limit=1)
    assert [i["id"] for i in page["items"]] == ["munich"] and page["total"] == 3
    # Other filters still narrow the list, and the sort choice does not undo the distance order.
    assert [i["id"] for i in _near(gallery, ROME, search="mu", sort="oldest")["items"]] == ["munich"]
    # Without near nothing changes: no distance, photos without a location included.
    plain = gallery["client"].get("/api/gallery/library").json()
    assert plain["total"] == 4 and all("distance_km" not in i for i in plain["items"])


def test_photos_at_the_same_spot_keep_the_order_they_were_taken_in(gallery):
    _photo(gallery, "second", KARLSRUHE, taken_at=datetime(2020, 5, 2))
    _photo(gallery, "undated", KARLSRUHE)
    _photo(gallery, "first", KARLSRUHE, taken_at=datetime(2020, 5, 1))
    _photo(gallery, "next-door", (KARLSRUHE[0] + 0.001, KARLSRUHE[1]), taken_at=datetime(1999, 1, 1))
    assert [i["id"] for i in _near(gallery, KARLSRUHE)["items"]] == ["first", "second", "undated", "next-door"]


def test_near_must_be_a_position(gallery):
    for bad in ("Karlsruhe", "91,0", "1;2"):
        resp = gallery["client"].get("/api/gallery/library", params={"near": bad})
        assert resp.status_code == 400 and "latitude,longitude" in resp.json()["detail"]


# ---------------------------------------------------------------- map

def test_the_map_counts_photos_per_spot(gallery):
    _photo(gallery, "k1", KARLSRUHE)
    _photo(gallery, "k2", (KARLSRUHE[0] + 0.00002, KARLSRUHE[1] - 0.00002))   # 3 m away: same spot
    _photo(gallery, "m", MUNICH)
    _photo(gallery, "no-gps")
    _photo(gallery, "deleted", ROME, is_active=False)
    _photo(gallery, "bobs", ROME, owner="bob")

    data = gallery["client"].get("/api/gallery/map").json()
    assert sorted(data["points"]) == sorted([[KARLSRUHE[0], KARLSRUHE[1], 2], [MUNICH[0], MUNICH[1], 1]])
    assert (data["located"], data["photos"]) == (3, 4)
    assert gallery["client"].get("/api/gallery/map", headers={"x-user": "bob"}).json()["points"] == [[*ROME, 1]]


def test_the_map_and_nearest_first_read_only_the_location_index(gallery):
    with gallery["engine"].connect() as conn:
        def plan(sql):
            return " ".join(r[3] for r in conn.execute(text("EXPLAIN QUERY PLAN " + sql)))

        located = ("SELECT id, gps_lat, gps_lng, taken_at FROM gallery_images "
                   "WHERE is_active = 1 AND owner = 'alice' AND gps_lat IS NOT NULL")
        assert "COVERING INDEX ix_gallery_images_located" in plan(located)
        # Partial: a query that does not ask for a location never picks it.
        assert "ix_gallery_images_located" not in plan(
            "SELECT count(id) FROM gallery_images WHERE is_active = 1 AND owner = 'alice'")


# ---------------------------------------------------------------- place search

NOMINATIM_KARLSRUHE = [{
    "lat": "49.0068705", "lon": "8.4034195", "name": "Karlsruhe", "addresstype": "city",
    "display_name": "Karlsruhe, Baden-Württemberg, Deutschland",
    "boundingbox": ["48.9404", "49.0912", "8.2773", "8.5417"],
}, {"lat": "not a number", "lon": "1", "display_name": "broken"}]


def test_place_search_finds_places_by_name(gallery, monkeypatch):
    calls = []

    async def fake(text, language):
        calls.append((text, language))
        return NOMINATIM_KARLSRUHE

    monkeypatch.setattr(map_routes, "_nominatim_search", fake)
    client = gallery["client"]
    resp = client.get("/api/gallery/places", params={"q": "  karlsruhe "}, headers={"accept-language": "de-DE,de;q=0.9"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["places"] == [{
        "name": "Karlsruhe", "label": "Karlsruhe, Baden-Württemberg, Deutschland",
        "lat": 49.0068705, "lng": 8.4034195, "kind": "city", "bbox": [48.9404, 49.0912, 8.2773, 8.5417],
    }]
    assert calls == [("karlsruhe", "de-DE,de;q=0.9")]
    client.get("/api/gallery/places", params={"q": "Karlsruhe"}, headers={"accept-language": "de-DE,de;q=0.9"})
    assert len(calls) == 1   # asked once, then remembered


def test_typed_coordinates_need_no_lookup(gallery, monkeypatch):
    async def offline(text, language):
        raise AssertionError("no lookup expected")

    monkeypatch.setattr(map_routes, "_nominatim_search", offline)
    places = gallery["client"].get("/api/gallery/places", params={"q": "49.0069, 8.4037"}).json()["places"]
    assert [(p["lat"], p["lng"], p["kind"]) for p in places] == [(49.0069, 8.4037, "coordinates")]


def test_place_search_reports_when_it_cannot_reach_the_service(gallery, monkeypatch):
    async def down(text, language):
        raise map_routes.PlaceSearchUnavailable("timed out")

    monkeypatch.setattr(map_routes, "_nominatim_search", down)
    resp = gallery["client"].get("/api/gallery/places", params={"q": "Karlsruhe"})
    assert resp.status_code == 502 and "click the map" in resp.json()["detail"]
    assert gallery["client"].get("/api/gallery/places", params={"q": "   "}).status_code == 400
    assert gallery["client"].get("/api/gallery/places", params={"q": "x" * 161}).status_code == 422


def test_lookups_name_the_app_and_are_a_second_apart(monkeypatch):
    seen, waits = [], []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=NOMINATIM_KARLSRUHE)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(map_routes.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    real_sleep = asyncio.sleep

    async def no_wait(seconds):
        waits.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(map_routes.asyncio, "sleep", no_wait)
    monkeypatch.setattr(map_routes, "_nominatim_last", 0.0)

    async def twice():
        await map_routes._nominatim_search("Karlsruhe", "de")
        await map_routes._nominatim_search("Rom", "")

    asyncio.run(twice())
    assert len(seen) == 2 and len(waits) == 1 and 0.9 < waits[0] <= 1.0
    first = seen[0]
    assert first.headers["user-agent"] == map_routes.NOMINATIM_USER_AGENT
    assert first.headers["accept-language"] == "de"
    assert first.url.params["q"] == "Karlsruhe" and first.url.params["format"] == "jsonv2"
    assert "accept-language" not in seen[1].headers


def test_the_map_tab_loads_leaflet_from_this_server():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "js" / "gallery.js").read_text(encoding="utf-8")
    assets = re.findall(r"'(/static/lib/leaflet/[^']+)'", js)
    assert sorted(Path(a).name for a in assets) == ["leaflet.css", "leaflet.js"]
    for asset in assets:
        assert (root / asset.lstrip("/")).is_file(), asset
    assert 'data-tab="map"' in js and "unpkg" not in js and "cdnjs" not in js

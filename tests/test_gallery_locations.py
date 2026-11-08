"""Estimated locations: photos without GPS placed by the time they were taken."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import Base, GalleryImage
from routes.gallery import map_routes
from src.gallery_library.locations import estimate

T = datetime(2024, 8, 12, 12, 0)
ROME = ("41.902800", "12.496400")
FLORENCE = ("43.769600", "11.255800")
ROME_TOO = ("41.903500", "12.497000")     # 90 m from ROME


def _m(minutes):
    return T + timedelta(minutes=minutes)


# ---------------------------------------------------------------- the rule

def test_a_photo_takes_the_spot_of_one_taken_within_a_quarter_hour():
    located = [(_m(-10), *ROME), (_m(40), *FLORENCE)]
    assert estimate(located, [("a", T)]) == {"a": ROME}
    assert estimate(located, [("b", _m(32))]) == {"b": FLORENCE}     # 8 min before Florence wins
    assert estimate([(_m(-16), *ROME)], [("c", T)]) == {}            # one side, too long ago


def test_staying_in_one_place_bridges_up_to_an_hour():
    assert estimate([(_m(-50), *ROME), (_m(55), *ROME_TOO)], [("a", T)]) == {"a": ROME}
    # Moving between two towns: no guess in between.
    assert estimate([(_m(-50), *ROME), (_m(55), *FLORENCE)], [("a", T)]) == {}
    assert estimate([(_m(-70), *ROME), (_m(55), *ROME_TOO)], [("a", T)]) == {}


# ---------------------------------------------------------------- the run

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
    monkeypatch.setattr(map_routes, "_estimate_cache", {})
    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    return {"db": session_local, "client": TestClient(app), "engine": engine}


def _photo(gallery, image_id, when, at=None, camera="Canon", owner="alice", **fields):
    db = gallery["db"]()
    db.add(GalleryImage(id=image_id, filename=f"{image_id}.jpg", prompt=image_id, owner=owner, is_active=True,
                        taken_at=when, camera_make=camera, source_id="src", source_relpath=f"{image_id}.jpg",
                        gps_lat=at[0] if at else None, gps_lng=at[1] if at else None, **fields))
    db.commit()
    db.close()


def _row(gallery, image_id):
    db = gallery["db"]()
    try:
        img = db.get(GalleryImage, image_id)
        return img.gps_lat, img.gps_lng, img.location_source
    finally:
        db.close()


def test_placing_photos_by_time_and_taking_it_back(gallery):
    client = gallery["client"]
    _photo(gallery, "phone", _m(-5), ROME, camera="Google")
    _photo(gallery, "camera", T)
    _photo(gallery, "screenshot", _m(1), camera=None)               # no camera: its date may be a file date
    _photo(gallery, "much-later", _m(300))
    _photo(gallery, "bobs-camera", T, owner="bob")
    assert client.get("/api/gallery/locations/estimate").json() == {"missing": 2, "possible": 1, "estimated": 0}

    from routes.gallery.gallery_helpers import library_signature
    db = gallery["db"]()
    before = library_signature(db, "alice")
    done = client.post("/api/gallery/locations/estimate").json()
    assert done == {"placed": 1, "missing": 1, "possible": 0, "estimated": 1}
    assert library_signature(db, "alice") != before     # trips are worked out again
    db.close()
    assert _row(gallery, "camera") == (*ROME, "time") and _row(gallery, "bobs-camera") == (None, None, None)
    # It shows like any located photo, marked as estimated.
    near = client.get("/api/gallery/library", params={"near": "41.9,12.5"}).json()["items"]
    assert {i["id"]: i["location_estimated"] for i in near} == {"phone": False, "camera": True}
    assert client.get("/api/gallery/map").json()["located"] == 2

    undone = client.delete("/api/gallery/locations/estimate").json()
    assert undone["cleared"] == 1 and _row(gallery, "camera") == (None, None, None)
    assert _row(gallery, "phone") == (*ROME, None)


def test_an_estimate_never_places_another_photo(gallery):
    client = gallery["client"]
    _photo(gallery, "phone", _m(-10), ROME, camera="Google")
    _photo(gallery, "first", T)
    client.post("/api/gallery/locations/estimate")
    _photo(gallery, "second", _m(12))      # 12 min after "first", 22 min after the phone photo
    assert client.post("/api/gallery/locations/estimate").json()["placed"] == 0
    assert _row(gallery, "second") == (None, None, None)


def test_estimated_locations_are_found_on_their_own_index(gallery):
    with gallery["engine"].connect() as conn:
        plan = " ".join(r[3] for r in conn.execute(text(
            "EXPLAIN QUERY PLAN SELECT id FROM gallery_images WHERE location_source IS NOT NULL AND owner = 'alice'")))
        assert "COVERING INDEX ix_gallery_images_estimated" in plan
        assert "ix_gallery_images_estimated" not in " ".join(r[3] for r in conn.execute(text(
            "EXPLAIN QUERY PLAN SELECT id FROM gallery_images WHERE owner = 'alice' AND is_active = 1")))

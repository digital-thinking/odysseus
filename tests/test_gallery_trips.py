"""Gallery trips, "On this day" and the date-taken range filter."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import Base, GalleryAlbum, GalleryImage
from routes.gallery import map_routes, trip_routes
from src.gallery_library import places
from src.gallery_library.trips import find_trips

KARLSRUHE = (49.0069, 8.4037)
ROME = (41.9028, 12.4964)
FLORENCE = (43.7696, 11.2558)
STUTTGART = (48.7758, 9.1829)      # 60 km from Karlsruhe: far enough for a day trip


@pytest.fixture
def gallery(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import routes.gallery.gallery_routes as gallery_routes
    for module in (gallery_routes, map_routes, trip_routes):
        monkeypatch.setattr(module, "SessionLocal", session_local)
        monkeypatch.setattr(module, "get_current_user", lambda request: request.headers.get("x-user", "alice"))
    monkeypatch.setattr(trip_routes, "_trip_cache", {})
    monkeypatch.setattr(trip_routes, "_day_cache", {})

    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    return {"db": session_local, "client": TestClient(app), "engine": engine}


_n = 0


def _photo(gallery, when, at=None, **fields):
    global _n
    _n += 1
    image_id = fields.pop("image_id", f"p{_n}")
    db = gallery["db"]()
    lat, lng = at if at else (None, None)
    db.add(GalleryImage(id=image_id, filename=f"{image_id}.jpg", prompt=image_id, owner=fields.pop("owner", "alice"),
                        is_active=True, taken_at=when, source_id=fields.pop("source_id", "src"),
                        gps_lat=None if lat is None else f"{lat:.6f}", gps_lng=None if lng is None else f"{lng:.6f}",
                        **fields))
    db.commit()
    db.close()
    return image_id


def _album(gallery, name, source_id="src", **fields):
    db = gallery["db"]()
    album = GalleryAlbum(id=f"album-{name}", name=name, owner="alice", source_id=source_id,
                         source_folder=name, **fields)
    db.add(album)
    db.commit()
    db.close()
    return f"album-{name}"


def _home_days(gallery, first: datetime, days: int, away=()):
    for d in range(days):
        if (first + timedelta(days=d)).day in away:
            continue
        _photo(gallery, first + timedelta(days=d, hours=12), KARLSRUHE)


def _trips(gallery):
    resp = gallery["client"].get("/api/gallery/trips")
    assert resp.status_code == 200, resp.text
    return resp.json()["trips"]


# ---------------------------------------------------------------- finding trips

def _pt(i, when, at):
    return (f"x{i}", when, at[0], at[1])


def test_a_trip_runs_from_leaving_home_to_coming_back():
    day = datetime(2024, 5, 1)
    points = [_pt(i, day + timedelta(days=i, hours=10), KARLSRUHE) for i in range(30) if not 9 <= i <= 12]
    # 10 May: breakfast at home, then off to Rome; back home in the evening of 13 May.
    points += [_pt(100, datetime(2024, 5, 10, 7), KARLSRUHE), _pt(101, datetime(2024, 5, 10, 15), ROME),
               _pt(102, datetime(2024, 5, 11, 12), ROME),     # 12 May: no photos — still on the trip
               _pt(103, datetime(2024, 5, 13, 9), FLORENCE), _pt(104, datetime(2024, 5, 13, 20), KARLSRUHE)]
    trips = find_trips(points)
    assert len(trips) == 1
    trip = trips[0]
    assert trip.start == datetime(2024, 5, 10, 7, 0, 0, 1) and trip.end == datetime(2024, 5, 13, 20)
    assert [p[0] for p in trip.points] == ["x101", "x102", "x103"] and trip.days == 4
    assert trip.far_km == pytest.approx(places._km(*KARLSRUHE, *ROME), rel=0.02)


def test_days_at_home_split_trips_and_short_trips_count():
    day = datetime(2024, 1, 1)
    points = [_pt(i, day + timedelta(days=i, hours=10), KARLSRUHE) for i in range(60) if i not in (20, 21, 40)]
    points += [_pt(200, day + timedelta(days=20, hours=11), ROME), _pt(201, day + timedelta(days=21, hours=11), ROME),
               _pt(202, day + timedelta(days=40, hours=11), STUTTGART)]
    trips = find_trips(points)
    assert [(t.start.date(), t.days) for t in trips] == [(datetime(2024, 1, 21).date(), 2),
                                                        (datetime(2024, 2, 10).date(), 1)]


def test_home_follows_a_move():
    day = datetime(2020, 1, 1)
    # Two years in Karlsruhe, then two years in Rome: neither town becomes one long trip.
    points = [_pt(i, day + timedelta(days=2 * i, hours=10), KARLSRUHE if i < 365 else ROME) for i in range(730)]
    trips = find_trips(points)
    assert sum(t.days for t in trips) < 120


def test_towns_are_named_offline():
    assert places.nearest_town(*KARLSRUHE)["name"] == "Karlsruhe"
    assert places.nearest_town(*ROME)["country"] == "IT"
    # A spot just outside a city goes by the city, not the village next to it.
    assert places.nearest_town(49.03, 8.36)["name"] == "Karlsruhe"
    assert places.nearest_town(0.0, -30.0) is None       # middle of the Atlantic


# ---------------------------------------------------------------- the trips list

def test_trips_are_named_after_where_they_went(gallery):
    _home_days(gallery, datetime(2024, 4, 1), 40, away={20, 21, 22})
    for d in range(3):
        for h in (10, 14, 18):
            _photo(gallery, datetime(2024, 4, 20 + d, h), ROME if d < 2 else FLORENCE)
    _photo(gallery, datetime(2024, 4, 21, 16), camera_model="EOS 350D")   # no location, taken during the trip

    trips = _trips(gallery)
    assert len(trips) == 1
    trip = trips[0]
    assert (trip["kind"], trip["photos"], trip["located"], trip["days"]) == ("located", 10, 9, 3)
    assert trip["countries"] == ["IT"] and trip["home_country"] == "DE" and trip["album"] is None
    assert trip["places"] == ["Rome", "Florence"]
    assert trip["start"].startswith("2024-04-20T00:00") and trip["end"].startswith("2024-04-23T00:00")
    assert 1 <= len(trip["covers"]) <= 4 and all(c.endswith("/thumb") for c in trip["covers"])

    # Opening the trip in Photos: everything taken in that time, with or without a location.
    resp = gallery["client"].get("/api/gallery/library",
                                 params={"since": trip["start"], "until": trip["end"], "sort": "taken_asc"})
    assert resp.status_code == 200 and resp.json()["total"] == 10


def test_a_trip_takes_the_name_of_the_album_most_of_it_is_in(gallery):
    italy = _album(gallery, "Italien 2024")
    year = _album(gallery, "2024")
    _home_days(gallery, datetime(2024, 4, 1), 40, away={20})
    for h in range(10):
        _photo(gallery, datetime(2024, 4, 20, 8 + h), ROME, album_id=italy if h < 6 else year)
    assert _trips(gallery)[0]["album"] == {"id": italy, "name": "Italien 2024"}


def test_folder_albums_from_before_locations_are_trips(gallery):
    ireland = _album(gallery, "Irland 2011")
    for d in range(10):
        _photo(gallery, datetime(2011, 7, 1 + d, 12), album_id=ireland)
    everyday = _album(gallery, "Gossau 2014")          # a whole year: not one trip
    for m in range(1, 13):
        _photo(gallery, datetime(2014, m, 3), album_id=everyday)
    by_hand = _album(gallery, "Birthday", source_id=None)
    for h in range(9):
        _photo(gallery, datetime(2012, 3, 3, 10 + h), album_id=by_hand)

    trips = _trips(gallery)
    assert [(t["kind"], t["album"]["name"], t["days"], t["photos"]) for t in trips] == [
        ("album", "Irland 2011", 10, 10)]


def test_trips_are_worked_out_again_only_after_a_change(gallery, monkeypatch):
    _home_days(gallery, datetime(2024, 4, 1), 40, away={20, 25})
    for h in range(8):
        _photo(gallery, datetime(2024, 4, 20, 8 + h), ROME)
    runs = []
    real = trip_routes.compute_trips
    monkeypatch.setattr(trip_routes, "compute_trips", lambda db, user: runs.append(user) or real(db, user))

    assert len(_trips(gallery)) == 1 and len(_trips(gallery)) == 1 and runs == ["alice"]
    for h in range(8):
        _photo(gallery, datetime(2024, 4, 25, 8 + h), FLORENCE)
    assert len(_trips(gallery)) == 2 and runs == ["alice", "alice"]


def test_a_renamed_trip_keeps_its_name(gallery):
    _home_days(gallery, datetime(2024, 4, 1), 40, away={20, 21, 22, 30})
    for d in (20, 21):
        for h in range(5):
            _photo(gallery, datetime(2024, 4, d, 9 + h), ROME)
    for h in range(8):
        _photo(gallery, datetime(2024, 4, 30, 9 + h), FLORENCE)
    rome = next(t for t in _trips(gallery) if t["main_place"] == "Rome")
    client = gallery["client"]

    resp = client.put("/api/gallery/trips/name",
                      json={"start": rome["start"], "end": rome["end"], "name": "  Rom   mit Anna "})
    assert resp.status_code == 200 and resp.json() == {"name": "Rom mit Anna"}
    names = {t["main_place"]: t.get("name") for t in _trips(gallery)}
    assert names == {"Rome": "Rom mit Anna", "Florence": None}

    # New photos make the trip a day longer: it keeps its name.
    for h in range(4):
        _photo(gallery, datetime(2024, 4, 22, 9 + h), FLORENCE)
    longer = next(t for t in _trips(gallery) if t["start"] == rome["start"])
    assert longer["end"] > rome["end"] and longer["name"] == "Rom mit Anna"

    # Renamed again, then back to the name worked out.
    client.put("/api/gallery/trips/name", json={"start": longer["start"], "end": longer["end"], "name": "Italien"})
    assert [t.get("name") for t in _trips(gallery) if t["start"] == rome["start"]] == ["Italien"]
    db = gallery["db"]()
    assert db.execute(text("SELECT owner, name FROM gallery_trip_names")).fetchall() == [("alice", "Italien")]
    db.close()
    assert client.put("/api/gallery/trips/name",
                      json={"start": longer["start"], "end": longer["end"], "name": ""}).json() == {"name": None}
    assert all("name" not in t for t in _trips(gallery))

    assert client.put("/api/gallery/trips/name", json={"start": "soon", "end": rome["end"]}).status_code == 400
    assert client.put("/api/gallery/trips/name", json={"start": rome["end"], "end": rome["start"]}).status_code == 400


def test_places_you_go_to_again_and_again_are_visits(gallery):
    _home_days(gallery, datetime(2024, 1, 1), 120, away={5, 12, 19, 26})
    for month in (1, 2, 3):
        for day in (5, 12, 19, 26):
            for h in range(8):
                _photo(gallery, datetime(2024, month, day, 9 + h), STUTTGART if day != 26 or month != 3 else ROME)
    trips = _trips(gallery)
    regular = {t["main_place"] for t in trips if t["regular"]}
    assert regular == {"Stuttgart"} and [t["main_place"] for t in trips if not t["regular"]] == ["Rome"]


def test_trips_are_per_user(gallery):
    _home_days(gallery, datetime(2024, 4, 1), 40)
    for h in range(8):
        _photo(gallery, datetime(2024, 4, 20, 8 + h), ROME)
    assert gallery["client"].get("/api/gallery/trips", headers={"x-user": "bob"}).json()["trips"] == []


# ---------------------------------------------------------------- date range and On this day

def test_the_date_range_filter(gallery):
    _photo(gallery, datetime(2023, 11, 7, 23, 59), image_id="before")
    _photo(gallery, datetime(2023, 11, 8, 0, 0), image_id="first")
    _photo(gallery, datetime(2023, 11, 8, 18, 30), image_id="last")
    _photo(gallery, datetime(2023, 11, 9, 0, 0), image_id="after")
    data = gallery["client"].get("/api/gallery/library",
                                 params={"since": "2023-11-08", "until": "2023-11-09", "sort": "taken_asc"}).json()
    assert [i["id"] for i in data["items"]] == ["first", "last"] and data["total"] == 2
    for bad in ({"since": "yesterday"}, {"until": "2023-13-01"}):
        resp = gallery["client"].get("/api/gallery/library", params=bad)
        assert resp.status_code == 400 and "ISO date" in resp.json()["detail"]


def test_on_this_day_lists_earlier_years(gallery):
    _photo(gallery, datetime(2023, 11, 8, 9), image_id="2023a")
    _photo(gallery, datetime(2023, 11, 8, 19), image_id="2023b")
    _photo(gallery, datetime(2019, 11, 8, 12), image_id="2019")
    _photo(gallery, datetime(2026, 11, 8, 8), image_id="today")       # this year: not a memory
    _photo(gallery, datetime(2023, 11, 9, 12), image_id="next-day")
    _photo(gallery, datetime(2022, 11, 8, 12), image_id="bobs", owner="bob")

    data = gallery["client"].get("/api/gallery/on-this-day", params={"date": "2026-11-08"}).json()
    years = [(y["year"], y["years_ago"], y["count"], [i["id"] for i in y["items"]]) for y in data["years"]]
    assert years == [(2023, 3, 2, ["2023a", "2023b"]), (2019, 7, 1, ["2019"])]
    assert gallery["client"].get("/api/gallery/on-this-day", params={"date": "8.11."}).status_code == 400


def test_date_ranges_read_only_that_part_of_the_index(gallery):
    with gallery["engine"].connect() as conn:
        plan = " ".join(r[3] for r in conn.execute(text(
            "EXPLAIN QUERY PLAN SELECT count(id) FROM gallery_images WHERE owner = 'alice' AND is_active = 1 "
            "AND coalesce(taken_at, created_at) >= '2024-01-01' AND coalesce(taken_at, created_at) < '2024-02-01'")))
    assert "ix_gallery_images_browse" in plan and "<expr>>?" in plan

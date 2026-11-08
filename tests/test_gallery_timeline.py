"""The timeline beside the photo grid: photos per month, in the grid's order."""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import Base, GalleryAlbum, GalleryImage


@pytest.fixture
def gallery(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    import routes.gallery.gallery_routes as gallery_routes
    monkeypatch.setattr(gallery_routes, "SessionLocal", session_local)
    monkeypatch.setattr(gallery_routes, "get_current_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())

    db = session_local()
    db.add(GalleryAlbum(id="a", name="Summer", owner="alice"))
    db.flush()
    taken = [datetime(2024, 8, 3), datetime(2024, 8, 20), datetime(2024, 6, 1), datetime(2023, 12, 31, 23, 59),
             datetime(2023, 1, 5)]
    for i, when in enumerate(taken):
        db.add(GalleryImage(id=f"p{i}", filename=f"p{i}.jpg", prompt=f"p{i}", owner="alice", is_active=True,
                            source_id="src", source_relpath=f"{i}.jpg", taken_at=when,
                            album_id="a" if i < 2 else None))
    db.add(GalleryImage(id="bobs", filename="b.jpg", prompt="b", owner="bob", is_active=True, source_id="src",
                        source_relpath="b.jpg", taken_at=datetime(2022, 1, 1)))
    db.commit()
    db.close()
    return TestClient(app)


def _months(client, **params):
    resp = client.get("/api/gallery/library", params={"months": "true", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_photos_per_month_in_the_order_of_the_grid(gallery):
    newest = _months(gallery, sort="recent")
    assert newest == {"months": [["2024-08", 2], ["2024-06", 1], ["2023-12", 1], ["2023-01", 1]], "total": 5}
    assert _months(gallery, sort="taken_asc")["months"] == newest["months"][::-1]
    # A month's first photo sits at the sum of the months before it, so the rail can jump there.
    page = gallery.get("/api/gallery/library", params={"sort": "recent", "offset": 3, "limit": 1}).json()
    assert [i["id"] for i in page["items"]] == ["p3"]


def test_the_timeline_follows_the_filters(gallery):
    assert _months(gallery, sort="recent", album="a") == {"months": [["2024-08", 2]], "total": 2}
    assert _months(gallery, sort="recent", since="2024-01-01")["total"] == 3


def test_orders_without_dates_have_no_timeline(gallery):
    assert _months(gallery, sort="shuffle") == {"months": [], "total": 0}
    assert _months(gallery, near="49,8") == {"months": [], "total": 0}

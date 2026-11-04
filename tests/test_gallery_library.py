"""Read-only library sources for the gallery.

The source folder must never be modified: every test that indexes or edits
snapshots the tree (paths, sizes, mtimes, SHA-256) before and after.
"""
import hashlib
import json
import os
import time
from datetime import datetime
from io import BytesIO

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as core_db
from core.database import Base, GalleryAlbum, GalleryImage, GalleryImageTag, GalleryLibrarySource
from src.gallery_library import files as lib_files
from src.gallery_library import paths as lib_paths

HOST_ROOT = "H:\\HiDrive\\Pictures\\Privat"


def _jpeg(path, size=(64, 48), color=(200, 30, 30), exif_date=None, orientation=None, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if exif_date:
        exif[306] = exif_date
    if orientation:
        exif[274] = orientation
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", exif=exif.tobytes())
    path.write_bytes(buf.getvalue())
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _snapshot(root):
    snap = {}
    for dirpath, dirs, files in os.walk(root):
        for d in dirs:
            snap[os.path.relpath(os.path.join(dirpath, d), root) + "/"] = None
        for f in files:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            snap[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns,
                                              hashlib.sha256(open(p, "rb").read()).hexdigest())
    return snap


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "library" / "privat"
    photo_mtime = datetime(2005, 5, 16, 19, 2).timestamp()
    _jpeg(root / "2005" / "2 Juni" / "Foto(01).jpg", mtime=photo_mtime)
    _jpeg(root / "2005" / "04 sep" / "Foto(01).jpg", size=(80, 40), color=(10, 120, 10),
          exif_date="2005:09:04 12:30:00", orientation=6)
    (root / "2005" / "Thumbs.db").write_bytes(b"windows thumbnail cache")
    (root / "2005" / "04 sep" / "clip.3gp").write_bytes(b"\x00video")
    _jpeg(root / "2006" / "later.jpg")

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import routes.gallery.gallery_routes as gallery_routes
    import routes.gallery.library_routes as library_routes
    from src.gallery_library import embeddings

    monkeypatch.setattr(core_db, "SessionLocal", session_local)
    for module in (gallery_routes, library_routes):
        monkeypatch.setattr(module, "SessionLocal", session_local)
        monkeypatch.setattr(module, "get_current_user", lambda request: request.headers.get("x-user", "alice"))
    monkeypatch.setattr(library_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(lib_files, "THUMB_DIR", str(tmp_path / "data" / "gallery_library" / "thumbs"))
    monkeypatch.setattr(embeddings, "delete_ids", lambda ids: None)
    monkeypatch.setenv(lib_paths.ENV_VAR, f"{HOST_ROOT}={root}")

    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    return {"root": root, "db": session_local, "client": TestClient(app), "tmp": tmp_path,
            "thumbs": tmp_path / "data" / "gallery_library" / "thumbs", "photo_mtime": photo_mtime}


def _add_source(lib, path=HOST_ROOT + "\\2005", **body):
    return lib["client"].post("/api/gallery/sources", json={"path": path, **body})


def _scan(lib, source_id):
    from src.gallery_library.scanner import scan_source
    return scan_source(source_id)["stats"]


# ---------------------------------------------------------------- paths

def test_mount_maps_windows_host_paths_and_confines(lib, tmp_path):
    root = lib["root"]
    assert lib_paths.resolve_source_path(HOST_ROOT) == os.path.realpath(root)
    assert lib_paths.resolve_source_path("h:/hidrive/pictures/privat/2005/") == os.path.realpath(root / "2005")
    assert lib_paths.resolve_source_path(str(root / "2006")) == os.path.realpath(root / "2006")
    assert lib_paths.display_path(str(root / "2005")) == HOST_ROOT + "\\2005"

    outside = tmp_path / "secret"
    outside.mkdir()
    for bad in ("H:\\HiDrive\\Pictures\\Other", "C:\\Windows", str(outside),
                HOST_ROOT + "\\..\\..\\..", HOST_ROOT + "\\does-not-exist"):
        with pytest.raises(ValueError):
            lib_paths.resolve_source_path(bad)
    # a symlink inside the library that points outside it is not followed out
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        lib_paths.resolve_source_path(HOST_ROOT + "\\escape")
    with pytest.raises(ValueError):
        lib_paths.confine_to_source(str(root), "../secret")


def test_no_mounts_configured_is_a_clear_error(monkeypatch):
    monkeypatch.delenv(lib_paths.ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="No library folders are configured"):
        lib_paths.resolve_source_path("/anything")


# ---------------------------------------------------------------- scanning

def test_scan_indexes_in_place_and_never_touches_the_source(lib):
    before = _snapshot(lib["root"])
    resp = _add_source(lib, path=HOST_ROOT, include_paths=["2005"])
    assert resp.status_code == 200, resp.text
    source_id = resp.json()["source"]["id"]

    stats = _scan(lib, source_id)
    assert (stats["found"], stats["new"], stats["skipped_videos"], stats["skipped_other"], stats["errors"]) == (2, 2, 1, 1, 0)
    assert _snapshot(lib["root"]) == before  # nothing written, renamed or touched

    db = lib["db"]()
    rows = {r.source_relpath: r for r in db.query(GalleryImage).all()}
    db.close()
    assert set(rows) == {"2005/2 Juni/Foto(01).jpg", "2005/04 sep/Foto(01).jpg"}  # 2006 not included
    no_exif = rows["2005/2 Juni/Foto(01).jpg"]
    assert no_exif.taken_at == datetime.fromtimestamp(lib["photo_mtime"])  # mtime fallback
    assert no_exif.model == "library" and no_exif.prompt == "Foto(01).jpg"
    rotated = rows["2005/04 sep/Foto(01).jpg"]
    assert rotated.taken_at == datetime(2005, 9, 4, 12, 30)
    assert (rotated.width, rotated.height) == (40, 80)  # EXIF orientation 6 swaps axes
    for row in rows.values():
        assert row.filename.startswith("lib-") and row.source_state == "ok"
        thumb = lib["thumbs"] / f"{row.id}.jpg"
        assert thumb.is_file()
        assert lib["root"] not in thumb.parents


def test_rescan_is_incremental_and_tracks_missing_hidden_and_returning_files(lib):
    source_id = _add_source(lib, path=HOST_ROOT, include_paths=["2005"]).json()["source"]["id"]
    _scan(lib, source_id)
    stats = _scan(lib, source_id)
    assert (stats["new"], stats["updated"], stats["unchanged"]) == (0, 0, 2)

    gone = lib["root"] / "2005" / "2 Juni" / "Foto(01).jpg"
    kept = gone.read_bytes(), os.stat(gone).st_mtime
    gone.unlink()  # the *user* removed it from their folder
    assert _scan(lib, source_id)["missing"] == 1
    db = lib["db"]()
    row = db.query(GalleryImage).filter(GalleryImage.source_relpath == "2005/2 Juni/Foto(01).jpg").one()
    assert (row.is_active, row.source_state) == (False, "missing")
    db.close()

    gone.write_bytes(kept[0])
    os.utime(gone, (kept[1], kept[1]))
    assert _scan(lib, source_id)["reactivated"] == 1

    # A photo hidden in Odysseus stays hidden on rescans.
    db = lib["db"]()
    row = db.query(GalleryImage).filter(GalleryImage.source_relpath == "2005/2 Juni/Foto(01).jpg").one()
    resp = lib["client"].delete(f"/api/gallery/{row.id}")
    db.close()
    assert resp.status_code == 200
    _scan(lib, source_id)
    db = lib["db"]()
    row = db.query(GalleryImage).filter(GalleryImage.source_relpath == "2005/2 Juni/Foto(01).jpg").one()
    assert (row.is_active, row.source_state) == (False, "hidden")
    db.close()
    assert gone.is_file()


# ---------------------------------------------------------------- routes

def test_library_routes_serve_filter_and_stay_read_only(lib):
    client, root = lib["client"], lib["root"]
    before = _snapshot(root)

    assert _add_source(lib, path="H:\\Somewhere\\Else").status_code == 400
    resp = _add_source(lib, path=HOST_ROOT, include_paths=["2005"], name="Privat")
    assert resp.status_code == 200
    source = resp.json()["source"]
    assert source["path"] == HOST_ROOT and source["read_only"] is True
    assert _add_source(lib, path=HOST_ROOT).status_code == 409
    _scan(lib, source["id"])

    listing = client.get("/api/gallery/sources").json()
    assert listing["sources"][0]["photo_count"] == 2 and listing["mounts"] == [HOST_ROOT]

    folders = client.get("/api/gallery/folders").json()["folders"]
    assert {(f["path"], f["count"]) for f in folders} == {("", 2), ("2005", 2), ("2005/04 sep", 1), ("2005/2 Juni", 1)}

    page = client.get("/api/gallery/library", params={"source": source["id"], "folder": "2005/04 sep"}).json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["read_only"] is True and item["source_path"] == "2005/04 sep/Foto(01).jpg"
    assert item["url"] == f"/api/gallery/{item['id']}/file"
    assert item["thumb_url"] == f"/api/gallery/{item['id']}/thumb"

    original = client.get(item["url"])
    assert original.status_code == 200 and original.headers["content-type"] == "image/jpeg"
    assert original.content == (root / "2005" / "04 sep" / "Foto(01).jpg").read_bytes()
    thumb = client.get(item["thumb_url"])
    assert thumb.status_code == 200 and Image.open(BytesIO(thumb.content)).size[0] <= 512
    # another user cannot read it
    assert client.get(item["url"], headers={"x-user": "bob"}).status_code == 404

    by_date = client.get("/api/gallery/library", params={"source": source["id"], "sort": "taken_asc"}).json()
    assert [i["source_path"] for i in by_date["items"]] == ["2005/2 Juni/Foto(01).jpg", "2005/04 sep/Foto(01).jpg"]

    # write operations on originals are refused; renaming only changes Odysseus's label
    assert client.post(f"/api/gallery/{item['id']}/rotate", json={"angle": 90}).status_code == 409
    replace = client.post(f"/api/gallery/{item['id']}/replace", files={"image": ("x.jpg", b"new", "image/jpeg")})
    assert replace.status_code == 409
    assert client.post(f"/api/gallery/{item['id']}/rename", json={"name": "Hike"}).json()["name"] == "Hike"

    # deleting hides the photo in Odysseus only
    assert client.delete(f"/api/gallery/{item['id']}").status_code == 200
    assert client.get(item["url"]).status_code == 404
    assert not (lib["thumbs"] / f"{item['id']}.jpg").exists()

    # forgetting the folder drops the index, not the photos
    removed = client.delete(f"/api/gallery/sources/{source['id']}").json()
    assert removed["removed_photos"] == 2
    db = lib["db"]()
    assert db.query(GalleryImage).count() == 0 and db.query(GalleryLibrarySource).count() == 0
    db.close()
    assert _snapshot(root) == before


def test_smart_search_ranks_by_similarity_and_keeps_filters(lib, monkeypatch):
    from src.gallery_library import embeddings
    source_id = _add_source(lib, path=HOST_ROOT, include_paths=["2005"]).json()["source"]["id"]
    _scan(lib, source_id)
    db = lib["db"]()
    ids = {r.source_relpath: r.id for r in db.query(GalleryImage).all()}
    db.close()
    sep, juni = ids["2005/04 sep/Foto(01).jpg"], ids["2005/2 Juni/Foto(01).jpg"]
    calls = []

    def fake_search(owner, query, k=200):
        calls.append((owner, query))
        return [(sep, 0.31), (juni, 0.12)]

    monkeypatch.setattr(embeddings, "search", fake_search)
    client = lib["client"]
    ranked = client.get("/api/gallery/library", params={"search": "Kinder im Schnee", "semantic": "true"}).json()
    assert ranked["semantic"] is True and [i["id"] for i in ranked["items"]] == [sep, juni]
    assert calls == [("alice", "Kinder im Schnee")]
    narrowed = client.get("/api/gallery/library", params={
        "search": "x", "semantic": "true", "source": source_id, "folder": "2005/2 Juni"}).json()
    assert [i["id"] for i in narrowed["items"]] == [juni]

    def unavailable(*_a, **_k):
        raise embeddings.SmartSearchUnavailable("vector store down")

    monkeypatch.setattr(embeddings, "search", unavailable)
    assert client.get("/api/gallery/library", params={"search": "x", "semantic": "true"}).status_code == 503


def _ids_by_path(lib):
    db = lib["db"]()
    try:
        return {r.source_relpath: r.id for r in db.query(GalleryImage).all()}
    finally:
        db.close()


def test_similar_photos_are_the_nearest_neighbours_of_one_photo(lib, monkeypatch):
    from src.gallery_library import embeddings
    source_id = _add_source(lib, path=HOST_ROOT).json()["source"]["id"]
    _scan(lib, source_id)
    ids = _ids_by_path(lib)
    sep, juni, later = ids["2005/04 sep/Foto(01).jpg"], ids["2005/2 Juni/Foto(01).jpg"], ids["2006/later.jpg"]
    calls = []

    def fake_similar(owner, image_id, k=200):
        calls.append((owner, image_id))
        return [(later, 0.93), (juni, 0.41)]

    monkeypatch.setattr(embeddings, "similar", fake_similar)
    client = lib["client"]
    body = client.get("/api/gallery/library", params={"similar_to": sep}).json()
    assert [i["id"] for i in body["items"]] == [later, juni] and body["total"] == 2   # most similar first
    assert calls == [("alice", sep)]
    narrowed = client.get("/api/gallery/library", params={"similar_to": sep, "source": source_id, "folder": "2005"}).json()
    assert [i["id"] for i in narrowed["items"]] == [juni]
    # someone else's photo can't be used to look into the index
    assert client.get("/api/gallery/library", params={"similar_to": sep}, headers={"x-user": "bob"}).status_code == 404

    def not_indexed(owner, image_id, k=200):
        raise embeddings.NotIndexed(image_id)

    monkeypatch.setattr(embeddings, "similar", not_indexed)
    resp = client.get("/api/gallery/library", params={"similar_to": sep})
    assert resp.status_code == 409 and "smart-search index" in resp.json()["detail"]


def test_similar_asks_the_ann_index_with_the_photos_own_vector(monkeypatch):
    from src.gallery_library import embeddings

    class FakeCollection:
        def __init__(self):
            self.queries = []

        def get(self, ids, include):
            return {"ids": ids, "embeddings": [[0.6, 0.8]] if ids == ["a"] else []}

        def count(self):
            return 3

        def query(self, **kwargs):
            self.queries.append(kwargs)
            return {"ids": [["a", "b", "c"]], "distances": [[0.0, 0.1, 0.5]]}

    col = FakeCollection()
    monkeypatch.setattr(embeddings, "_collection", lambda: col)
    assert embeddings.similar("alice", "a", k=5) == [("b", pytest.approx(0.9)), ("c", pytest.approx(0.5))]
    (query,) = col.queries
    assert query["query_embeddings"] == [[0.6, 0.8]] and query["where"] == {"owner": "alice"}
    assert query["n_results"] == 3   # never more than the index holds
    with pytest.raises(embeddings.NotIndexed):
        embeddings.similar("alice", "not-indexed")


def test_tag_counts_list_most_used_first_and_open_exactly_those_photos(lib):
    source_id = _add_source(lib, path=HOST_ROOT).json()["source"]["id"]
    _scan(lib, source_id)
    db = lib["db"]()
    rows = {r.source_relpath: r for r in db.query(GalleryImage).all()}
    rows["2005/2 Juni/Foto(01).jpg"].ai_tags = "man, beach, blue sky"
    rows["2005/04 sep/Foto(01).jpg"].ai_tags = "woman, beach"
    rows["2005/04 sep/Foto(01).jpg"].tags = "Beach, holiday"   # own tag, other case: still one photo
    rows["2006/later.jpg"].ai_tags = "young man, beach"
    db.commit()
    ids = {path: row.id for path, row in rows.items()}
    db.close()

    client = lib["client"]
    body = client.get("/api/gallery/tag-counts").json()
    assert body["total"] == 6
    assert [(t["tag"], t["count"]) for t in body["tags"]] == [
        ("beach", 3), ("blue sky", 1), ("holiday", 1), ("man", 1), ("woman", 1), ("young man", 1)]
    assert body["tags"][0]["cover_url"] == f"/api/gallery/{ids['2006/later.jpg']}/thumb"   # newest photo
    found = client.get("/api/gallery/tag-counts", params={"q": "MAN"}).json()
    assert [t["tag"] for t in found["tags"]] == ["man", "woman", "young man"]
    paged = client.get("/api/gallery/tag-counts", params={"offset": 1, "limit": 2}).json()
    assert [t["tag"] for t in paged["tags"]] == ["blue sky", "holiday"] and paged["total"] == 6

    # opening a tag shows exactly the photos counted: "man" is not "woman" or "young man"
    man = client.get("/api/gallery/library", params={"exact_tag": "man"}).json()
    assert [i["id"] for i in man["items"]] == [ids["2005/2 Juni/Foto(01).jpg"]]
    assert client.get("/api/gallery/library", params={"exact_tag": "Beach"}).json()["total"] == 3
    assert client.get("/api/gallery/library", params={"exact_tag": "%"}).json()["total"] == 0
    assert client.get("/api/gallery/tag-counts", headers={"x-user": "bob"}).json() == {"tags": [], "total": 0}


def _tag_rows(lib):
    db = lib["db"]()
    try:
        return sorted((r.tag, r.image_id) for r in db.query(GalleryImageTag).all())
    finally:
        db.close()


def _photo(db, image_id="p1", **fields):
    row = GalleryImage(id=image_id, filename=f"{image_id}.jpg", prompt=fields.pop("prompt", "x"),
                       owner=fields.pop("owner", "alice"), is_active=True, **fields)
    db.add(row)
    db.commit()
    return row


def test_the_tag_index_follows_every_change_to_a_photo(lib):
    db = lib["db"]()
    row = _photo(db, tags="Beach, sunset", ai_tags=" beach ,blue sky")
    assert _tag_rows(lib) == [("beach", "p1"), ("blue sky", "p1"), ("sunset", "p1")]   # one row per tag

    row.ai_tags = "forest"
    db.commit()
    assert _tag_rows(lib) == [("beach", "p1"), ("forest", "p1"), ("sunset", "p1")]

    row.tags = ""
    db.commit()
    assert _tag_rows(lib) == [("forest", "p1")]

    db.query(GalleryImage).filter(GalleryImage.id == "p1").update({"ai_tags": "lake"}, synchronize_session=False)
    db.commit()
    assert _tag_rows(lib) == [("lake", "p1")]

    db.query(GalleryImage).filter(GalleryImage.id == "p1").delete(synchronize_session=False)
    db.commit()
    db.close()
    assert _tag_rows(lib) == []


def test_tags_with_quotes_and_backslashes_cannot_break_the_tag_index(lib):
    db = lib["db"]()
    row = _photo(db, ai_tags='say "cheese", back\\slash, it\'s, [x]')
    assert [tag for tag, _ in _tag_rows(lib)] == sorted(['say "cheese"', "back\\slash", "it's", "[x]"])

    row.ai_tags = ""
    db.commit()
    db.close()
    assert _tag_rows(lib) == []


def test_tag_filters_and_search_keep_their_meaning(lib):
    db = lib["db"]()
    _photo(db, "a", ai_tags="young man, sandy beach", prompt="IMG_1.jpg")
    _photo(db, "b", ai_tags="woman, forest", prompt="IMG_2.jpg")
    _photo(db, "c", tags="Man", prompt="strandurlaub.jpg")
    _photo(db, "other", owner="bob", ai_tags="man, beach")
    db.close()

    def found(**params):
        body = lib["client"].get("/api/gallery/library", params=params).json()
        assert body["total"] == len(body["items"])
        return sorted(i["id"] for i in body["items"])

    assert found(exact_tag="man") == ["c"]
    assert found(tag="man") == ["a", "b", "c"]            # the tag pills match part of a tag
    assert found(tag="man,forest") == ["b"]               # several pills narrow it down
    assert found(search="MAN") == ["a", "b", "c"]
    assert found(search="young beach") == ["a"]           # every word must match, in any tag
    assert found(search="strand") == ["c"]                # the file name counts too
    assert found(search="beach,") == ["a"]                # a stray comma does not hide the tag
    assert found(search="%") == [] and found(tag="_") == []


def test_only_the_first_page_counts_the_tagged_photos(lib):
    db = lib["db"]()
    _photo(db, "a", ai_tags="beach")
    _photo(db, "b", ai_tags="")
    db.close()

    client = lib["client"]
    assert client.get("/api/gallery/library", params={"limit": 1}).json()["total_tagged"] == 1
    assert "total_tagged" not in client.get("/api/gallery/library", params={"limit": 1, "offset": 1}).json()


def test_tag_counts_are_reused_until_a_photo_changes(lib, monkeypatch):
    from routes.gallery import tag_stats

    db = lib["db"]()
    row = _photo(db, ai_tags="beach")
    builds = []
    real_build = tag_stats._build
    monkeypatch.setattr(tag_stats, "_build", lambda *args: builds.append(1) or real_build(*args))

    def counts():
        return {t["tag"]: t["count"] for t in lib["client"].get("/api/gallery/tag-counts").json()["tags"]}

    assert counts() == {"beach": 1} and counts() == {"beach": 1}
    assert len(builds) == 1

    row.ai_tags = "beach, lake"
    db.commit()
    assert counts() == {"beach": 1, "lake": 1}

    row.is_active = False        # hiding a photo takes it out of the counts
    db.commit()
    assert counts() == {}

    db.delete(row)
    db.commit()
    db.close()
    assert counts() == {}
    assert len(builds) == 4


def test_a_repaired_photo_is_picked_up_even_with_the_same_size_and_date(lib):
    photo = lib["root"] / "2006" / "later.jpg"
    good = photo.read_bytes()
    stamp = photo.stat().st_mtime
    photo.write_bytes(b"\x00" * len(good))
    os.utime(photo, (stamp, stamp))
    source_id = _add_source(lib, path=HOST_ROOT).json()["source"]["id"]

    first = _scan(lib, source_id)
    assert (first["errors"], first["unreadable"]) == (1, 1)
    second = _scan(lib, source_id)
    assert (second["errors"], second["unreadable"]) == (0, 1)   # known, so not reported again

    photo.write_bytes(good)
    os.utime(photo, (stamp, stamp))
    repaired = _scan(lib, source_id)

    assert (repaired["new"], repaired["errors"], repaired["unreadable"]) == (1, 0, 0)


def test_a_removed_photo_shows_in_the_tag_counts_without_a_login(lib, monkeypatch):
    # With auth off there is no owner filter, so the change check has to count rows on its own.
    from routes.gallery import gallery_helpers, tag_stats

    monkeypatch.setattr(gallery_helpers, "_auth_disabled", lambda: True)
    db = lib["db"]()
    _photo(db, "a", owner=None, ai_tags="beach")
    _photo(db, "b", owner=None, ai_tags="beach")
    assert tag_stats.current(lib["db"], None).counts == {"beach": 2}

    db.query(GalleryImage).filter(GalleryImage.id == "b").delete(synchronize_session=False)
    db.commit()
    db.close()

    assert tag_stats.current(lib["db"], None).counts == {"beach": 1}


def test_the_migration_fills_the_tag_index_of_an_existing_database(lib, monkeypatch, tmp_path):
    import sqlite3

    db = lib["db"]()
    _photo(db, tags="own", ai_tags="beach, Blue Sky")
    _photo(db, "p2", ai_tags='say "hi"')
    db.close()
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER gallery_images_tags_ai")
    conn.execute("DROP INDEX ix_gallery_images_browse")
    conn.execute("DELETE FROM gallery_image_tags")
    conn.commit()
    conn.close()

    monkeypatch.setattr(core_db, "DATABASE_URL", f"sqlite:///{path}")
    core_db._migrate_gallery_tag_index()

    assert _tag_rows(lib) == [("beach", "p1"), ("blue sky", "p1"), ("own", "p1"), ('say "hi"', "p2")]
    db = lib["db"]()
    _photo(db, "p3", ai_tags="new")     # the trigger is back too
    db.close()
    assert ("new", "p3") in _tag_rows(lib)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ix_gallery_images_browse'").fetchone()
    conn.close()


def test_searches_use_the_indexes_instead_of_reading_every_photo(lib):
    from sqlalchemy import text as sql

    db = lib["db"]()

    def plan(statement):
        return " ".join(row[3] for row in db.execute(sql("EXPLAIN QUERY PLAN " + statement)))

    active = "gallery_images.is_active = 1 AND gallery_images.owner = 'alice'"
    page = ("SELECT * FROM gallery_images WHERE " + active +
            " ORDER BY coalesce(gallery_images.taken_at, gallery_images.created_at) DESC,"
            " gallery_images.source_relpath DESC LIMIT 60")
    assert "ix_gallery_images_browse" in plan(page) and "TEMP B-TREE" not in plan(page)
    assert "ix_gallery_images_ai_tagged" in plan(
        "SELECT count(*) FROM gallery_images WHERE " + active +
        " AND gallery_images.ai_tags IS NOT NULL AND gallery_images.ai_tags != ''")
    assert "COVERING INDEX ix_gallery_faces_owner_person" in plan(
        "SELECT person_id, count(id), count(DISTINCT image_id) FROM gallery_faces "
        "WHERE owner = 'alice' AND ignored != 1 AND person_id IS NOT NULL GROUP BY person_id")
    db.close()


def _albums(lib):
    return {a["name"]: a for a in lib["client"].get("/api/gallery/albums").json()["albums"]}


def _trip_source(lib):
    """A library of trips, as in a holiday folder: one folder per trip."""
    root = lib["root"]
    _jpeg(root / "2016_BadGastein" / "a.jpg")
    _jpeg(root / "2016_BadGastein" / "Handy" / "b.jpg")
    _jpeg(root / "2018_Italien_Frankreich" / "c.jpg")
    _jpeg(root / "loose.jpg")
    source_id = _add_source(lib, path=HOST_ROOT).json()["source"]["id"]
    _scan(lib, source_id)
    return source_id


def _turn_on_albums(lib, source_id):
    return lib["client"].patch(f"/api/gallery/sources/{source_id}", json={"sync": {"albums": True}})


def test_album_names_read_like_titles():
    from src.gallery_library.albums import album_name

    assert album_name("2016_BadGastein") == "Bad Gastein 2016"
    assert album_name("2016_BadGasteinHapimag") == "Bad Gastein Hapimag 2016"
    assert album_name("2018_Italien_Frankreich") == "Italien Frankreich 2018"
    assert album_name("2025_Ägypten") == "Ägypten 2025"
    assert album_name("Schöne_Blumen") == "Schöne Blumen"
    assert album_name("2023") == "2023"


def test_folder_albums_are_off_unless_asked_for(lib, fake_index):
    from src.gallery_library import jobs

    source_id = _trip_source(lib)
    jobs.run_now(source_id, embed=False, wait_seconds=1)
    assert _albums(lib) == {}


def test_turning_on_folder_albums_files_every_photo_by_top_folder(lib):
    source_id = _trip_source(lib)
    assert _turn_on_albums(lib, source_id).status_code == 200

    albums = _albums(lib)
    assert albums["Bad Gastein 2016"]["count"] == 2    # subfolders count towards the trip
    assert albums["Italien Frankreich 2018"]["count"] == 1
    assert "loose" not in " ".join(albums)             # a photo in the root belongs to no trip
    in_album = lib["client"].get("/api/gallery/library", params={"album": albums["Bad Gastein 2016"]["id"]}).json()
    assert in_album["total"] == 2


def test_folder_albums_list_the_newest_trip_first_after_hand_made_ones(lib):
    source_id = _trip_source(lib)
    _turn_on_albums(lib, source_id)
    db = lib["db"]()
    db.add(GalleryAlbum(id="mine", name="Favourites", owner="alice"))
    db.commit()
    db.close()

    names = [a["name"] for a in lib["client"].get("/api/gallery/albums").json()["albums"]]
    assert names[0] == "Favourites"
    assert names.index("Italien Frankreich 2018") < names.index("Bad Gastein 2016")


def test_the_nightly_scan_keeps_folder_albums_up_to_date(lib, fake_index):
    from src.gallery_library import jobs

    source_id = _trip_source(lib)
    _turn_on_albums(lib, source_id)
    _jpeg(lib["root"] / "2016_BadGastein" / "new.jpg")
    _jpeg(lib["root"] / "2026_Japan" / "tokyo.jpg")

    result = jobs.run_now(source_id, embed=False, wait_seconds=1)

    assert result["result"]["albums"] == {"created": 1, "added": 2, "removed": 0}
    albums = _albums(lib)
    assert albums["Bad Gastein 2016"]["count"] == 3 and albums["Japan 2026"]["count"] == 1


def test_a_renamed_album_and_a_photo_filed_elsewhere_are_left_alone(lib):
    from src.gallery_library.albums import sync_albums

    source_id = _trip_source(lib)
    _turn_on_albums(lib, source_id)
    client = lib["client"]
    album = _albums(lib)["Bad Gastein 2016"]
    client.put(f"/api/gallery/albums/{album['id']}", json={"name": "Skiing"})
    db = lib["db"]()
    db.add(GalleryAlbum(id="best", name="Best of", owner="alice"))
    db.flush()
    db.query(GalleryImage).filter(GalleryImage.source_relpath == "2016_BadGastein/a.jpg").update({"album_id": "best"})
    db.commit()
    db.close()

    assert sync_albums(source_id) == {"created": 0, "added": 0, "removed": 0}

    albums = _albums(lib)
    assert albums["Skiing"]["count"] == 1 and albums["Best of"]["count"] == 1


def test_a_deleted_folder_album_stays_deleted_until_albums_are_turned_on_again(lib):
    from src.gallery_library.albums import sync_albums

    source_id = _trip_source(lib)
    _turn_on_albums(lib, source_id)
    client = lib["client"]
    client.delete(f"/api/gallery/albums/{_albums(lib)['Bad Gastein 2016']['id']}")

    assert sync_albums(source_id)["created"] == 0
    assert "Bad Gastein 2016" not in _albums(lib)

    client.patch(f"/api/gallery/sources/{source_id}", json={"sync": {"albums": False}})
    _turn_on_albums(lib, source_id)
    assert _albums(lib)["Bad Gastein 2016"]["count"] == 2


def test_an_album_goes_when_its_folder_is_gone(lib):
    import shutil

    from src.gallery_library.albums import sync_albums

    source_id = _trip_source(lib)
    _turn_on_albums(lib, source_id)
    shutil.rmtree(lib["root"] / "2018_Italien_Frankreich")
    _scan(lib, source_id)

    assert sync_albums(source_id)["removed"] == 1
    assert "Italien Frankreich 2018" not in _albums(lib)


def test_removing_a_library_folder_removes_its_albums(lib):
    source_id = _trip_source(lib)
    _turn_on_albums(lib, source_id)

    lib["client"].delete(f"/api/gallery/sources/{source_id}")

    assert _albums(lib) == {}


def test_folder_albums_never_touch_the_source_folder(lib):
    before = _snapshot(lib["root"])
    source_id = _trip_source(lib)
    after_scan = _snapshot(lib["root"])
    _turn_on_albums(lib, source_id)
    assert _snapshot(lib["root"]) == after_scan and len(after_scan) > len(before)


def test_background_job_scans_and_reports(lib):
    from src.gallery_library import jobs
    source_id = _add_source(lib, path=HOST_ROOT, include_paths=["2005"]).json()["source"]["id"]
    resp = lib["client"].post(f"/api/gallery/sources/{source_id}/scan", json={"embed": False, "tag": False})
    assert resp.status_code == 200
    deadline = time.time() + 20
    while jobs.is_running() and time.time() < deadline:
        time.sleep(0.05)
    status = lib["client"].get("/api/gallery/sources/job").json()["job"]
    assert status["state"] == "done", status
    assert status["result"]["scan"]["new"] == 2


def test_smart_search_reports_a_loading_or_broken_query_model(monkeypatch):
    import threading
    from src.gallery_library import embeddings

    monkeypatch.setattr(embeddings, "_text_ready", threading.Event())
    monkeypatch.setattr(embeddings, "_text_warming", False)
    monkeypatch.setattr(embeddings, "_text_error", None)
    monkeypatch.setattr(embeddings, "_SEARCH_WAIT_SECONDS", 0.2)
    release = threading.Event()
    monkeypatch.setattr(embeddings, "_text_embedder", lambda: release.wait(5))

    with pytest.raises(embeddings.SmartSearchUnavailable, match="still loading"):
        embeddings.search("alice", "beach")  # loads in the background; the request is not held hostage
    release.set()
    assert embeddings.warm_up(wait_seconds=2) is True

    def broken():
        raise OSError("no internet")

    monkeypatch.setattr(embeddings, "_text_ready", threading.Event())
    monkeypatch.setattr(embeddings, "_text_embedder", broken)
    embeddings.warm_up(wait_seconds=1)
    with pytest.raises(embeddings.SmartSearchUnavailable, match="no internet"):
        embeddings.search("alice", "beach")


# ---------------------------------------------------------------- auto-sync

@pytest.fixture
def fake_index(monkeypatch):
    """Stand-in for the vector store so jobs can run their embedding phase."""
    from src.gallery_library import embeddings, sync
    stored, deleted = set(), []
    monkeypatch.setattr(embeddings, "missing_ids", lambda ids: [i for i in ids if i not in stored])
    monkeypatch.setattr(embeddings, "index_images", lambda items, **kw: stored.update(i for i, *_ in items) or len(items))
    monkeypatch.setattr(embeddings, "warm_up", lambda wait_seconds=0: True)
    monkeypatch.setattr(embeddings, "delete_ids", lambda ids: deleted.extend(ids))
    monkeypatch.setattr(sync, "_PROGRESS_POLL_SECONDS", 0.02)
    return {"stored": stored, "deleted": deleted}


def _wait_for_job():
    from src.gallery_library import jobs
    deadline = time.time() + 20
    while jobs.is_running() and time.time() < deadline:
        time.sleep(0.05)
    return jobs.status()


def _age_last_scan(lib, source_id, minutes):
    from datetime import timedelta
    db = lib["db"]()
    src = db.query(GalleryLibrarySource).filter(GalleryLibrarySource.id == source_id).one()
    src.last_scan_at = src.last_scan_at - timedelta(minutes=minutes)
    db.commit()
    db.close()


def _run_index_task(owner="alice", progress=None):
    import asyncio
    from src.builtin_actions import action_index_gallery_library
    return asyncio.run(action_index_gallery_library(owner, progress_cb=progress))


def test_index_task_picks_up_new_photos_and_stays_quiet_when_nothing_changed(lib, fake_index):
    from src.builtin_actions import TaskNoop
    _add_source(lib, path=HOST_ROOT, include_paths=["2005"], name="Privat")
    progress = []
    message, ok = _run_index_task(progress=progress.append)
    assert ok and message == "Privat: 2 new, 2 added to smart search"

    _jpeg(lib["root"] / "2005" / "Neu" / "IMG_0001.jpg", color=(1, 2, 3))  # a photo arrives
    message, ok = _run_index_task()
    assert ok and message == "Privat: 1 new, 1 added to smart search"
    db = lib["db"]()
    new = db.query(GalleryImage).filter(GalleryImage.source_relpath == "2005/Neu/IMG_0001.jpg").one()
    db.close()
    assert new.is_active and new.id in fake_index["stored"]

    with pytest.raises(TaskNoop):   # nothing new: no Activity entry
        _run_index_task()
    with pytest.raises(TaskNoop):   # someone without library folders
        _run_index_task(owner="bob")


def test_index_task_yields_to_the_user_and_stops_its_indexing_thread(lib, fake_index, monkeypatch):
    """The scheduler cancels background tasks when Odysseus is used; the indexing
    thread must stop too, not keep using the CPU/GPU behind a 'stopped' run."""
    import asyncio
    from src.builtin_actions import action_index_gallery_library
    from src.gallery_library import embeddings, jobs
    monkeypatch.setattr(embeddings, "index_images", lambda items, cancel=None, **kw: (cancel.wait(10), 0)[1])
    _add_source(lib, path=HOST_ROOT, include_paths=["2005"], name="Privat")

    async def scenario():
        run = asyncio.ensure_future(action_index_gallery_library("alice"))
        while not ((jobs.status() or {}).get("phase") == "embedding"):
            await asyncio.sleep(0.01)
        run.cancel()   # what stop_background_tasks_for_foreground does
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(scenario())
    assert _wait_for_job()["state"] == "cancelled"


def test_index_task_waiting_for_a_manual_scan_leaves_it_running_when_stopped(lib, fake_index, monkeypatch):
    """A long manual scan is still running when the nightly task fires; the task
    waits. If the user then opens Odysseus, only the waiting task stops — the
    manual scan carries on and no second run starts behind it."""
    import asyncio
    import threading
    from src.builtin_actions import action_index_gallery_library
    from src.gallery_library import embeddings, jobs
    release = threading.Event()
    monkeypatch.setattr(embeddings, "index_images", lambda items, cancel=None, **kw: (release.wait(10), len(items))[1])
    monkeypatch.setattr(jobs, "_WAIT_POLL_SECONDS", 0.02)
    source_id = _add_source(lib, path=HOST_ROOT, include_paths=["2005"], name="Privat").json()["source"]["id"]
    jobs.start(source_id, embed=True)   # the manual scan (Scan now)
    while (jobs.status() or {}).get("phase") != "embedding":
        time.sleep(0.01)

    async def scenario():
        run = asyncio.ensure_future(action_index_gallery_library("alice"))
        await asyncio.sleep(0.2)   # the task is waiting for the manual scan
        run.cancel()               # the user opens Odysseus
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(scenario())
    manual = jobs.status()
    assert (manual["trigger"], manual["state"]) == ("manual", "running")
    release.set()
    assert _wait_for_job()["state"] == "done"
    time.sleep(0.2)
    assert jobs.status()["trigger"] == "manual"   # the task's waiting thread gave up


def test_the_gallery_progress_poll_does_not_count_as_user_activity():
    from src.interactive_gate import should_track_interactive_request
    assert should_track_interactive_request("/api/gallery/sources/job", "GET") is False
    assert should_track_interactive_request("/api/gallery/library", "GET") is True


def test_a_broken_photo_is_reported_once_not_every_night(lib, fake_index):
    from src.builtin_actions import TaskNoop
    source_id = _add_source(lib, path=HOST_ROOT, include_paths=["2005"], name="Privat").json()["source"]["id"]
    broken = lib["root"] / "2005" / "kaputt.jpg"
    broken.write_bytes(b"")                       # an empty file in the photo folder
    message, ok = _run_index_task()
    assert not ok and "1 unreadable" in message   # newly broken: worth a look
    with pytest.raises(TaskNoop):                 # the same broken file again: a quiet night
        _run_index_task()
    stats = _scan(lib, source_id)
    assert (stats["errors"], stats["unreadable"], list(stats["unreadable_files"])) == (0, 1, ["2005/kaputt.jpg"])

    _jpeg(broken, color=(9, 9, 9))                # the original is restored
    stats = _scan(lib, source_id)
    assert (stats["new"], stats["errors"], stats["unreadable"], stats["unreadable_files"]) == (1, 0, 0, {})


def test_index_task_reports_an_unreachable_folder(lib, fake_index):
    import shutil
    _add_source(lib, path=HOST_ROOT, include_paths=["2005"], name="Privat")
    shutil.rmtree(lib["root"] / "2005")       # e.g. the drive went away
    message, ok = _run_index_task()
    assert not ok and message.startswith("Privat: failed")


def test_index_task_is_a_seeded_pausable_cron_task_that_keeps_a_custom_schedule(lib, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    import routes.prefs_routes as preferences
    from core.database import ScheduledTask
    from src.builtin_actions import BUILTIN_ACTIONS
    from src.task_scheduler import HOUSEKEEPING_DEFAULTS, TaskScheduler

    defs = HOUSEKEEPING_DEFAULTS["index_gallery_library"]
    assert (defs["schedule"], defs["cron_expression"]) == ("cron", "0 3 * * *")
    assert "index_gallery_library" in BUILTIN_ACTIONS
    monkeypatch.setattr(preferences, "_load_for_user", lambda owner: {"tasks_opened": True})
    scheduler = TaskScheduler(session_manager=None)
    monkeypatch.setattr(scheduler, "ensure_assistant_defaults", AsyncMock())

    asyncio.run(scheduler.ensure_defaults("alice"))
    db = lib["db"]()
    task = db.query(ScheduledTask).filter_by(owner="alice", action="index_gallery_library").one()
    assert (task.status, task.cron_expression, task.name) == ("active", "0 3 * * *", "Gallery Library Index")
    assert task.next_run is not None
    task.cron_expression, task.status = "30 1 * * 6", "paused"   # the user edits and pauses it
    db.commit()
    db.close()

    asyncio.run(scheduler.ensure_defaults("alice"))              # e.g. the next app start
    db = lib["db"]()
    task = db.query(ScheduledTask).filter_by(owner="alice", action="index_gallery_library").one()
    assert (task.cron_expression, task.status) == ("30 1 * * 6", "paused")
    db.close()
    info = lib["client"].get("/api/gallery/sources").json()["index_task"]
    assert (info["cron_expression"], info["status"], info["name"]) == ("30 1 * * 6", "paused", "Gallery Library Index")


def test_changed_photo_is_retagged_and_reembedded_but_untouched_ones_keep_tags(lib, fake_index):
    source_id = _add_source(lib, path=HOST_ROOT, include_paths=["2005"]).json()["source"]["id"]
    _scan(lib, source_id)
    db = lib["db"]()
    for row in db.query(GalleryImage).all():
        row.ai_tags, row.tags = "beach, sun", "family"
    db.commit()
    ids = {r.source_relpath: r.id for r in db.query(GalleryImage).all()}
    db.close()

    changed = lib["root"] / "2005" / "2 Juni" / "Foto(01).jpg"
    _jpeg(changed, color=(0, 0, 255), mtime=lib["photo_mtime"] + 60)   # the user edited it
    os.remove(lib["thumbs"] / f"{ids['2005/04 sep/Foto(01).jpg']}.jpg")  # only the thumbnail is gone

    from src.gallery_library import jobs
    jobs.start(source_id, embed=True, tag=False)
    job = _wait_for_job()
    assert (job["result"]["scan"]["updated"], job["result"]["scan"]["unchanged"]) == (1, 1)
    db = lib["db"]()
    rows = {r.source_relpath: r for r in db.query(GalleryImage).all()}
    db.close()
    assert rows["2005/2 Juni/Foto(01).jpg"].ai_tags == "" and rows["2005/2 Juni/Foto(01).jpg"].tags == "family"
    assert rows["2005/04 sep/Foto(01).jpg"].ai_tags == "beach, sun"
    assert ids["2005/2 Juni/Foto(01).jpg"] in fake_index["deleted"]          # stale vector dropped
    assert ids["2005/04 sep/Foto(01).jpg"] not in fake_index["deleted"]
    assert (lib["thumbs"] / f"{ids['2005/04 sep/Foto(01).jpg']}.jpg").is_file()  # thumbnail rebuilt


def test_a_folder_that_suddenly_looks_empty_is_not_emptied_in_the_gallery(lib):
    import shutil
    source_id = _add_source(lib, path=HOST_ROOT, include_paths=["2005"]).json()["source"]["id"]
    _scan(lib, source_id)
    for child in (lib["root"] / "2005").iterdir():  # e.g. a cloud drive that is not mounted
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    with pytest.raises(FileNotFoundError, match="looks empty"):
        _scan(lib, source_id)
    db = lib["db"]()
    assert db.query(GalleryImage).filter(GalleryImage.is_active == True).count() == 2  # noqa: E712
    db.close()


def test_adding_a_subfolder_keeps_existing_photos_and_tags(lib):
    client = lib["client"]
    source_id = _add_source(lib, path=HOST_ROOT, include_paths=["2005"]).json()["source"]["id"]
    _scan(lib, source_id)
    db = lib["db"]()
    for row in db.query(GalleryImage).all():
        row.ai_tags = "campfire"
    db.commit()
    db.close()
    assert client.patch(f"/api/gallery/sources/{source_id}", json={"include_paths": ["2005", "nope"]}).status_code == 400
    resp = client.patch(f"/api/gallery/sources/{source_id}", json={"include_paths": ["2005", "2006"]})
    assert resp.json()["source"]["include_paths"] == ["2005", "2006"]
    assert _scan(lib, source_id)["new"] == 1  # only 2006/later.jpg
    db = lib["db"]()
    tags = {r.source_relpath: r.ai_tags for r in db.query(GalleryImage).all()}
    db.close()
    assert tags["2005/04 sep/Foto(01).jpg"] == "campfire" and "2006/later.jpg" in tags


def test_scan_choices_are_remembered_for_the_index_task(lib, fake_index):
    client = lib["client"]
    source = _add_source(lib, path=HOST_ROOT, include_paths=["2005"]).json()["source"]
    assert source["sync"] == {"embed": True, "tag": False, "faces": False, "albums": False}
    patched = client.patch(f"/api/gallery/sources/{source['id']}", json={"sync": {"tag": True, "interval_minutes": 5}})
    assert patched.json()["source"]["sync"] == {
        "embed": True, "tag": True, "faces": False, "albums": False}   # unknown keys dropped
    client.post(f"/api/gallery/sources/{source['id']}/scan", json={"embed": False, "tag": True})
    _wait_for_job()
    listed = client.get("/api/gallery/sources").json()
    assert listed["sources"][0]["sync"] == {"embed": False, "tag": True, "faces": False, "albums": False}
    assert listed["index_task"] is None   # no scheduled task seeded in this test


# ---------------------------------------------------------------- tagging

def test_tagging_turns_thinking_off_for_local_models_and_retries_plain(monkeypatch):
    import src.gallery_tagging as tagging

    monkeypatch.setattr(tagging, "resolve_tag_model",
                        lambda owner: ("http://host.docker.internal:11434/v1/chat/completions", "qwen3.5:9b", {}))
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if body.get("reasoning_effort") == "none" and len(seen) == 1:
            return httpx.Response(400, json={"error": "unknown field"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Beach, Sunset., kids\nsand"}}]})

    real_client = httpx.Client
    monkeypatch.setattr(tagging.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    assert tagging.request_tags(b"img", "image/jpeg", "alice") == "beach, sunset, kids, sand"
    assert seen[0]["reasoning_effort"] == "none" and seen[0]["max_tokens"] == 300
    assert "reasoning_effort" not in seen[1]  # retried without the thinking switches


def test_tagging_leaves_hosted_models_alone(monkeypatch):
    import src.gallery_tagging as tagging
    payload = {"model": "gpt-4o"}
    assert tagging._thinking_off(payload, "https://api.openai.com/v1/chat/completions", "gpt-4o") is False
    assert payload == {"model": "gpt-4o"}

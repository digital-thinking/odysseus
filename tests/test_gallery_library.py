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
from core.database import Base, GalleryImage, GalleryLibrarySource
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


def test_the_gallery_progress_poll_does_not_count_as_user_activity():
    from src.interactive_gate import should_track_interactive_request
    assert should_track_interactive_request("/api/gallery/sources/job", "GET") is False
    assert should_track_interactive_request("/api/gallery/library", "GET") is True


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
    assert source["sync"] == {"embed": True, "tag": False}
    patched = client.patch(f"/api/gallery/sources/{source['id']}", json={"sync": {"tag": True, "interval_minutes": 5}})
    assert patched.json()["source"]["sync"] == {"embed": True, "tag": True}   # unknown keys dropped
    client.post(f"/api/gallery/sources/{source['id']}/scan", json={"embed": False, "tag": True})
    _wait_for_job()
    listed = client.get("/api/gallery/sources").json()
    assert listed["sources"][0]["sync"] == {"embed": False, "tag": True}
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

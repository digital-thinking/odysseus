"""People in gallery photos: faces are found, grouped, named and corrected.

A fake face engine stands in for the ONNX models: photo colour = identity
(red photos show one person, green another), so grouping is deterministic.
The source folder must stay untouched; crops live in Odysseus's data dir.
"""
import hashlib
import os
import time

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as core_db
from core.database import Base, GalleryFace, GalleryImage, GalleryPerson
from src.gallery_faces import engine as face_engine
from src.gallery_faces import index as face_index
from src.gallery_faces.engine import ARCFACE_DST, Face, _nms, _similarity_transform
from src.gallery_library import files as lib_files
from src.gallery_library import paths as lib_paths

HOST_ROOT = "H:\\Pictures\\Privat"
COLORS = {"red": (220, 30, 30), "green": (30, 200, 40), "blue": (30, 40, 210)}
_BASIS = {"red": 0, "green": 1, "blue": 2}


class FakeEngine:
    model_pack = "fake"

    def analyze(self, img):
        r, g, b = img.convert("RGB").resize((1, 1)).getpixel((0, 0))
        name = max(_BASIS, key=lambda k: {"red": r, "green": g, "blue": b}[k])
        rng = np.random.default_rng(r * 65536 + g * 256 + b)
        vec = np.zeros(512, dtype=np.float32)
        vec[_BASIS[name]] = 1.0
        vec += rng.normal(0, 0.01, 512).astype(np.float32)  # same person: similarity ~0.95
        vec /= np.linalg.norm(vec)
        return img, [Face(box=(50.0, 30.0, 130.0, 110.0), score=0.9, landmarks=np.zeros((5, 2)), embedding=vec)]


def _photo(path, color, shade=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = tuple(max(0, v - shade) for v in COLORS[color])
    Image.new("RGB", (200, 150), c).save(path, "JPEG")


def _snapshot(root):
    return {os.path.relpath(os.path.join(d, f), root): hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest()
            for d, _, fs in os.walk(root) for f in fs}


@pytest.fixture
def faces_env(tmp_path, monkeypatch):
    root = tmp_path / "library" / "privat"
    for i in range(3):
        _photo(root / "2005" / f"red{i}.jpg", "red", shade=i * 5)
        _photo(root / "2005" / f"green{i}.jpg", "green", shade=i * 5)
    _photo(root / "2005" / "blue0.jpg", "blue")

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import routes.gallery.gallery_routes as gallery_routes
    import routes.gallery.library_routes as library_routes
    import routes.gallery.people_routes as people_routes
    from src.gallery_library import embeddings

    monkeypatch.setattr(core_db, "SessionLocal", session_local)
    for module in (gallery_routes, library_routes, people_routes):
        monkeypatch.setattr(module, "SessionLocal", session_local)
        monkeypatch.setattr(module, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(library_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(lib_files, "THUMB_DIR", str(tmp_path / "data" / "gallery_library" / "thumbs"))
    monkeypatch.setattr(face_index, "CROP_DIR", str(tmp_path / "data" / "gallery_faces" / "crops"))
    monkeypatch.setattr(face_engine, "get_engine", lambda: FakeEngine())
    monkeypatch.setattr(embeddings, "delete_ids", lambda ids: None)
    monkeypatch.setenv(lib_paths.ENV_VAR, f"{HOST_ROOT}={root}")

    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    client = TestClient(app)
    source_id = client.post("/api/gallery/sources", json={"path": HOST_ROOT, "include_paths": ["2005"]}).json()["source"]["id"]
    return {"root": root, "db": session_local, "client": client, "source": source_id,
            "crops": tmp_path / "data" / "gallery_faces" / "crops"}


def _index(env):
    from src.gallery_library.scanner import scan_source
    scan_source(env["source"])
    stats = face_index.scan_faces(env["source"])
    stats.update(face_index.cluster_faces("alice"))
    return stats


def _faces_by_file(env):
    db = env["db"]()
    rows = (db.query(GalleryFace, GalleryImage).join(GalleryImage, GalleryImage.id == GalleryFace.image_id).all())
    db.close()
    return {img.source_relpath.split("/")[-1]: face for face, img in rows}


def test_faces_are_found_grouped_and_stored_outside_the_source(faces_env):
    before = _snapshot(faces_env["root"])
    stats = _index(faces_env)
    assert (stats["faces"], stats["new_people"], stats["grouped"]) == (7, 2, 6)
    assert _snapshot(faces_env["root"]) == before

    people = faces_env["client"].get("/api/gallery/people").json()
    assert sorted(p["face_count"] for p in people["people"]) == [3, 3]
    assert all(p["name"] is None for p in people["people"]) and people["unknown_faces"] == 1   # the lone blue face
    for face in _faces_by_file(faces_env).values():
        crop = faces_env["crops"] / f"{face.id}.jpg"
        assert crop.is_file() and faces_env["root"] not in crop.parents
    assert _index(faces_env)["faces"] == 0   # already scanned → nothing to do


def test_naming_a_face_names_its_group_and_new_photos_are_recognised(faces_env):
    client = faces_env["client"]
    _index(faces_env)
    red = _faces_by_file(faces_env)["red0.jpg"]
    resp = client.post(f"/api/gallery/faces/{red.id}", json={"name": "Lena"}).json()
    lena = resp["face"]["person_id"]
    people = {p["name"]: p for p in client.get("/api/gallery/people").json()["people"]}
    assert people["Lena"]["face_count"] == 3   # the whole red group, not just one face

    by_person = client.get("/api/gallery/library", params={"person": lena}).json()
    assert sorted(i["source_path"] for i in by_person["items"]) == [f"2005/red{i}.jpg" for i in range(3)]
    by_name = client.get("/api/gallery/library", params={"search": "lena"}).json()
    assert by_name["total"] == 3

    _photo(faces_env["root"] / "2005" / "red_new.jpg", "red", shade=2)   # a new photo of Lena arrives
    stats = _index(faces_env)
    assert stats["matched"] == 1
    face = _faces_by_file(faces_env)["red_new.jpg"]
    assert (face.person_id, face.assigned) == (lena, "auto")


def test_corrections_stick_and_same_names_merge(faces_env):
    client = faces_env["client"]
    _index(faces_env)
    faces = _faces_by_file(faces_env)
    lena = client.post(f"/api/gallery/faces/{faces['red0.jpg'].id}", json={"name": "Lena"}).json()["face"]["person_id"]

    # "that's not Lena" — the face is not put back automatically
    client.post(f"/api/gallery/faces/{faces['red2.jpg'].id}", json={"person_id": None})
    face_index.cluster_faces("alice")
    db = faces_env["db"]()
    assert db.get(GalleryFace, faces["red2.jpg"].id).person_id is None
    db.close()

    # "not a face"
    client.post(f"/api/gallery/faces/{faces['blue0.jpg'].id}", json={"ignored": True})
    assert client.get("/api/gallery/people").json()["unknown_faces"] == 1   # only red2 is left unknown

    # naming the green group "Lena" too merges it into Lena
    green_group = _faces_by_file(faces_env)["green0.jpg"].person_id
    resp = client.patch(f"/api/gallery/people/{green_group}", json={"name": "lena"}).json()
    assert resp["merged_into"] == lena
    people = client.get("/api/gallery/people").json()["people"]
    assert [(p["name"], p["face_count"]) for p in people] == [("Lena", 5)]

    # forgetting a person releases the faces to Unknown, permanently
    client.delete(f"/api/gallery/people/{lena}")
    face_index.cluster_faces("alice")
    assert client.get("/api/gallery/people").json() == {"people": [], "unknown_faces": 6}


def test_people_are_listed_by_photo_count_and_all_their_faces_can_be_paged(faces_env):
    client = faces_env["client"]
    _index(faces_env)
    green = _faces_by_file(faces_env)["green0.jpg"]
    client.post(f"/api/gallery/faces/{green.id}", json={"name": "Anna"})
    people = client.get("/api/gallery/people").json()["people"]
    assert [(p["name"], p["photo_count"]) for p in people] == [("Anna", 3), (None, 3)]   # a tie: named first

    _photo(faces_env["root"] / "2005" / "red3.jpg", "red", shade=3)
    _index(faces_env)
    people = client.get("/api/gallery/people").json()["people"]
    assert [(p["name"], p["photo_count"]) for p in people] == [(None, 4), ("Anna", 3)]   # most photos first

    url = f"/api/gallery/people/{people[0]['id']}/faces"
    first = client.get(url, params={"limit": 3}).json()
    rest = client.get(url, params={"offset": 3, "limit": 3}).json()
    assert (first["total"], first["photo_count"], len(first["faces"]), len(rest["faces"])) == (4, 4, 3, 1)
    assert len({f["id"] for f in first["faces"] + rest["faces"]}) == 4   # no face repeated or skipped
    unknown = client.get("/api/gallery/faces/unknown").json()
    assert unknown["total"] == 1 and len(unknown["faces"]) == 1


def test_photos_can_be_filtered_by_several_people_at_once(faces_env):
    client = faces_env["client"]
    _index(faces_env)
    faces = _faces_by_file(faces_env)
    lena = client.post(f"/api/gallery/faces/{faces['red0.jpg'].id}", json={"name": "Lena"}).json()["face"]["person_id"]
    tom = client.post(f"/api/gallery/faces/{faces['green0.jpg'].id}", json={"name": "Tom"}).json()["face"]["person_id"]
    db = faces_env["db"]()   # Tom is in red1.jpg too, next to Lena
    db.add(GalleryFace(id="tom-in-red1", owner="alice", image_id=faces["red1.jpg"].image_id, person_id=tom,
                       box_x=0.6, box_y=0.2, box_w=0.2, box_h=0.3, score=0.9, assigned="user"))
    db.commit()
    db.close()
    both = client.get("/api/gallery/library", params={"person": f"{lena},{tom}"}).json()
    assert [i["source_path"] for i in both["items"]] == ["2005/red1.jpg"]   # photos with all of them
    assert client.get("/api/gallery/library", params={"person": tom}).json()["total"] == 4


def test_photo_faces_have_relative_boxes_and_names(faces_env):
    client = faces_env["client"]
    _index(faces_env)
    face = _faces_by_file(faces_env)["green1.jpg"]
    client.post(f"/api/gallery/faces/{face.id}", json={"name": "Tom"})
    data = client.get(f"/api/gallery/{face.image_id}/faces").json()
    assert data["scanned"] is True
    (only,) = data["faces"]
    assert only["person_name"] == "Tom"
    assert only["box"] == pytest.approx({"x": 0.25, "y": 0.2, "w": 0.4, "h": 80 / 150})
    crop = client.get(only["crop_url"])
    assert crop.status_code == 200 and crop.headers["content-type"] == "image/jpeg"


def test_the_index_job_runs_the_people_phase(faces_env):
    from src.gallery_library import jobs
    jobs.start(faces_env["source"], embed=False, tag=False, faces=True)
    deadline = time.time() + 20
    while jobs.is_running() and time.time() < deadline:
        time.sleep(0.05)
    result = jobs.status()["result"]["faces"]
    assert (result["faces"], result["new_people"]) == (7, 2)


def test_one_in_between_face_does_not_merge_two_people():
    """Single linkage would chain A–bridge–B into one person; average linkage must not."""
    from types import SimpleNamespace
    rng = np.random.default_rng(7)

    def face(base, noise=0.02, score=0.9, w=0.5):
        v = base + rng.normal(0, noise, 512)
        return SimpleNamespace(id=str(rng.integers(1e9)), embedding=(v / np.linalg.norm(v)).astype(np.float32).tobytes(),
                               score=score, box_w=w)

    a, b = np.zeros(512), np.zeros(512)
    a[0], b[1] = 1.0, 1.0
    people_a = [face(a) for _ in range(4)]
    people_b = [face(b) for _ in range(4)]
    bridge = face((a + b) / np.sqrt(2), noise=0.0)   # ~0.7 similar to both
    groups = face_index._average_linkage_groups(people_a + people_b + [bridge])
    assert sorted(len(g) for g in groups) == [4, 5] or sorted(len(g) for g in groups) == [4, 4]
    assert not any(set(map(id, people_a)) & set(map(id, g)) and set(map(id, people_b)) & set(map(id, g)) for g in groups)

    # blurry or tiny faces may not start a person
    assert face_index._is_clear(face(a, score=0.9, w=0.5), image_width=640)
    assert not face_index._is_clear(face(a, score=0.6, w=0.5), image_width=640)
    assert not face_index._is_clear(face(a, score=0.9, w=0.05), image_width=640)   # 32 px


def test_alignment_and_nms_math():
    # template onto itself → identity; a scaled/shifted copy maps back exactly
    m = _similarity_transform(ARCFACE_DST, ARCFACE_DST)
    assert np.allclose(m, [[1, 0, 0], [0, 1, 0]], atol=1e-6)
    moved = ARCFACE_DST * 2.5 + np.array([40.0, -12.0])
    m = _similarity_transform(moved, ARCFACE_DST)
    mapped = moved @ m[:, :2].T + m[:, 2]
    assert np.allclose(mapped, ARCFACE_DST, atol=1e-4)
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], dtype=float)
    assert _nms(boxes, np.array([0.9, 0.8, 0.7]), 0.4) == [0, 2]

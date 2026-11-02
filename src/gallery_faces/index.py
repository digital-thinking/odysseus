"""Find faces in gallery photos and group them into people.

* ``scan_faces`` — detect + embed faces in library photos not yet scanned with
  the current model pack. Originals are only read; face crops go to
  ``data/gallery_faces/crops``.
* ``cluster_faces`` — assign faces to people: first to a known person when the
  face is clearly most similar to that person's faces, then group the rest
  into new unnamed people. Faces the user assigned are never moved.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections import defaultdict
from io import BytesIO
from typing import Callable, Optional

import numpy as np

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

FACE_DATA_DIR = os.path.join(DATA_DIR, "gallery_faces")
CROP_DIR = os.path.join(FACE_DATA_DIR, "crops")
CROP_SIZE = 160

MIN_SCORE = 0.6         # detector confidence for a face we keep
MIN_FACE_PX = 40        # smaller faces are too blurry to recognise reliably
MATCH_SIMILARITY = 0.5      # a clear face joins a known person at or above this (best of their faces)
MATCH_SIMILARITY_WEAK = 0.55 # a small/blurry face needs a closer match
MATCH_MARGIN = 0.05          # ... and must be clearly ahead of the next person
GROUP_SIMILARITY = 0.5       # average similarity for unknown faces to form a new person
MIN_GROUP_SIZE = 3           # faces needed to create a new (unnamed) person
# Only clear faces may start new people: blurry/tiny faces produce generic
# embeddings that resemble each other more than the actual person (measured on
# 2005 phone photos: mixed groups disappear with this gate).
GROUP_MIN_SCORE = 0.70
GROUP_MIN_FACE_PX = 60

Progress = Callable[[int, int], None]


def crop_path(face_id: str) -> str:
    return os.path.join(CROP_DIR, f"{face_id}.jpg")


def _vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _save_crop(face_id: str, img, box) -> None:
    from src.gallery_library.files import ensure_writable_derived_path
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    pad = 0.35 * max(w, h)
    side = max(w, h) + 2 * pad
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    region = (int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2))
    crop = img.crop(region).resize((CROP_SIZE, CROP_SIZE))
    os.makedirs(CROP_DIR, exist_ok=True)
    dest = ensure_writable_derived_path(crop_path(face_id))
    crop.save(dest, "JPEG", quality=88)


def scan_faces(source_id: str, *, progress: Optional[Progress] = None,
               cancel: Optional[threading.Event] = None) -> dict:
    """Detect faces in the source's photos that were not scanned with the current model."""
    from PIL import Image, ImageOps

    from core.database import GalleryFace, GalleryImage, SessionLocal
    from src.gallery_faces.engine import get_engine
    from src.gallery_library.files import library_file_path

    engine = get_engine()
    db = SessionLocal()
    try:
        todo = [r.id for r in db.query(GalleryImage.id).filter(
            GalleryImage.source_id == source_id,
            GalleryImage.is_active == True,  # noqa: E712
            (GalleryImage.faces_scanned == None) | (GalleryImage.faces_scanned != engine.model_pack),  # noqa: E711
        )]
    finally:
        db.close()

    stats = {"photos": 0, "faces": 0, "errors": 0}
    for index, image_id in enumerate(todo, start=1):
        if cancel is not None and cancel.is_set():
            break
        db = SessionLocal()
        try:
            img_row = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
            if img_row is None:
                continue
            with open(library_file_path(img_row, db), "rb") as fh:  # read-only
                img = ImageOps.exif_transpose(Image.open(BytesIO(fh.read()))).convert("RGB")
            work, faces = engine.analyze(img)
            # a rescan with another model replaces the old faces of this photo
            for old in db.query(GalleryFace).filter(GalleryFace.image_id == image_id).all():
                _remove_crop(old.id)
                db.delete(old)
            ww, wh = work.size
            for face in faces:
                x1, y1, x2, y2 = face.box
                if face.score < MIN_SCORE or (x2 - x1) < MIN_FACE_PX:
                    continue
                face_id = str(uuid.uuid4())
                _save_crop(face_id, work, face.box)
                db.add(GalleryFace(
                    id=face_id, owner=img_row.owner, image_id=image_id,
                    box_x=max(0.0, x1 / ww), box_y=max(0.0, y1 / wh),
                    box_w=min(1.0, (x2 - x1) / ww), box_h=min(1.0, (y2 - y1) / wh),
                    score=face.score, embedding=face.embedding.astype(np.float32).tobytes(),
                ))
                stats["faces"] += 1
            img_row.faces_scanned = engine.model_pack
            db.commit()
            stats["photos"] += 1
        except Exception as exc:  # one unreadable photo must not stop the run
            db.rollback()
            stats["errors"] += 1
            logger.warning("Face scan of %s failed: %s", image_id, exc)
        finally:
            db.close()
        if progress:
            progress(index, len(todo))
    return stats


def _remove_crop(face_id: str) -> None:
    try:
        os.remove(crop_path(face_id))
    except OSError:
        pass


def _is_clear(face, image_width) -> bool:
    return (face.score or 0) >= GROUP_MIN_SCORE and face.box_w * (image_width or 0) >= GROUP_MIN_FACE_PX


def _match_known(assigned: list, candidates: list, clear: dict) -> list:
    """Join candidates to known people (best similarity to any of their faces). Returns the rest."""
    if not assigned or not candidates:
        return candidates
    known = np.stack([_vec(f.embedding) for f in assigned])
    owners_of = [f.person_id for f in assigned]
    rest = []
    for start in range(0, len(candidates), 512):
        chunk = candidates[start:start + 512]
        sims = np.stack([_vec(f.embedding) for f in chunk]) @ known.T
        for face, row in zip(chunk, sims):
            best: dict[str, float] = defaultdict(lambda: -1.0)
            for pid, s in zip(owners_of, row):
                if s > best[pid]:
                    best[pid] = float(s)
            ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
            top_pid, top = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
            need = MATCH_SIMILARITY if clear[face.id] else MATCH_SIMILARITY_WEAK
            if top >= need and top - runner_up >= MATCH_MARGIN:
                face.person_id, face.assigned = top_pid, "auto"
                assigned.append(face)
                owners_of.append(top_pid)
            else:
                rest.append(face)
    return rest


def _average_linkage_groups(faces: list) -> list[list]:
    """Groups of faces whose average pairwise similarity is >= GROUP_SIMILARITY.

    Runs average linkage inside each connected component of the similarity
    graph (groups can never span components), which keeps it fast.
    """
    if len(faces) < MIN_GROUP_SIZE:
        return []
    emb = np.stack([_vec(f.embedding) for f in faces])
    sims = emb @ emb.T
    parent = list(range(len(faces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in zip(*np.where(np.triu(sims >= GROUP_SIMILARITY, 1))):
        x, y = find(a), find(b)
        if x != y:
            parent[x] = y
    components: dict[int, list[int]] = defaultdict(list)
    for i in range(len(faces)):
        components[find(i)].append(i)

    groups = []
    for members in components.values():
        if len(members) < MIN_GROUP_SIZE:
            continue
        clusters = [[m] for m in members]
        sums = sims[np.ix_(members, members)].astype(np.float64)
        np.fill_diagonal(sums, 0.0)
        sizes = np.ones(len(members))
        alive = list(range(len(members)))
        while len(alive) > 1:
            avg = sums[np.ix_(alive, alive)] / np.outer(sizes[alive], sizes[alive])
            np.fill_diagonal(avg, -9.0)
            a, b = np.unravel_index(np.argmax(avg), avg.shape)
            if avg[a, b] < GROUP_SIMILARITY:
                break
            keep, gone = alive[a], alive[b]
            sums[keep, :] += sums[gone, :]
            sums[:, keep] += sums[:, gone]
            sums[keep, keep] = 0.0
            sizes[keep] += sizes[gone]
            clusters[keep] += clusters[gone]
            alive.remove(gone)
        groups += [[faces[i] for i in clusters[k]] for k in alive if len(clusters[k]) >= MIN_GROUP_SIZE]
    return groups


def cluster_faces(owner: Optional[str]) -> dict:
    """Assign unassigned faces to people (existing or new). Returns counts.

    1. faces that clearly match a known person join them;
    2. clear faces that resemble each other (average linkage) become new people;
    3. the rest get another chance to join one of the new people.
    Faces the user assigned or took out of a person are never moved.
    """
    from core.database import GalleryFace, GalleryImage, GalleryPerson, SessionLocal

    db = SessionLocal()
    try:
        q = (db.query(GalleryFace, GalleryImage.width)
             .join(GalleryImage, GalleryImage.id == GalleryFace.image_id)
             .filter(GalleryImage.is_active == True, GalleryFace.ignored != True,  # noqa: E712
                     GalleryFace.embedding != None))  # noqa: E711
        if owner is not None:
            q = q.filter(GalleryFace.owner == owner)
        rows = q.all()
        stats = {"matched": 0, "new_people": 0, "grouped": 0}
        if not rows:
            return stats
        clear = {face.id: _is_clear(face, width) for face, width in rows}
        faces = [face for face, _ in rows]
        assigned = [f for f in faces if f.person_id]
        # assigned == "user" without a person: the user took it out of a person
        # ("that's not Lena") — never put it back automatically.
        unassigned = [f for f in faces if not f.person_id and f.assigned != "user"]

        before = len(unassigned)
        unassigned = _match_known(assigned, unassigned, clear)
        stats["matched"] = before - len(unassigned)

        for members in _average_linkage_groups([f for f in unassigned if clear[f.id]]):
            person = GalleryPerson(id=str(uuid.uuid4()), owner=members[0].owner,
                                   cover_face_id=max(members, key=lambda f: f.score or 0).id)
            db.add(person)
            # No ORM relationship orders face → person writes, so insert the
            # person before the faces point at it (SQLite enforces the FK).
            db.flush()
            for face in members:
                face.person_id, face.assigned = person.id, "cluster"
                assigned.append(face)
            stats["new_people"] += 1
            stats["grouped"] += len(members)

        leftover = [f for f in unassigned if not f.person_id]
        before = len(leftover)
        leftover = _match_known(assigned, leftover, clear)
        stats["matched"] += before - len(leftover)
        db.commit()
        return stats
    finally:
        db.close()


def delete_faces_of_images(db, image_ids: list[str]) -> int:
    """Remove faces (and crops) of the given photos; empty unnamed people are dropped."""
    from core.database import GalleryFace
    if not image_ids:
        return 0
    faces = db.query(GalleryFace).filter(GalleryFace.image_id.in_(image_ids)).all()
    for face in faces:
        _remove_crop(face.id)
        db.delete(face)
    db.flush()
    drop_empty_unnamed_people(db)
    return len(faces)


def drop_empty_unnamed_people(db) -> None:
    from core.database import GalleryFace, GalleryPerson
    used = {pid for (pid,) in db.query(GalleryFace.person_id).filter(GalleryFace.person_id != None).distinct()}  # noqa: E711
    for person in db.query(GalleryPerson).filter(GalleryPerson.name == None).all():  # noqa: E711
        if person.id not in used:
            db.delete(person)

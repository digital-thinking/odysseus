"""People in gallery photos: list, name, merge and correct recognised faces.

Faces are found by the library index (People option per folder). Naming one
face or group re-runs grouping so similar unknown faces join that person.
"""

import asyncio
import os
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func

from core.database import GalleryFace, GalleryImage, GalleryPerson, SessionLocal
from routes.gallery.gallery_helpers import _image_urls, _owner_filter
from src.auth_helpers import get_current_user
from src.gallery_faces.index import cluster_faces, crop_path, drop_empty_unnamed_people

FILE_HEADERS = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}


def _face_dict(face: GalleryFace, person: Optional[GalleryPerson], img: Optional[GalleryImage]) -> Dict[str, Any]:
    return {
        "id": face.id,
        "image_id": face.image_id,
        "person_id": face.person_id,
        "person_name": person.name if person else None,
        "box": {"x": face.box_x, "y": face.box_y, "w": face.box_w, "h": face.box_h},
        "score": face.score,
        "assigned": face.assigned,
        "crop_url": f"/api/gallery/faces/{face.id}/crop",
        "image_thumb_url": _image_urls(img)[1] if img else None,
    }


def _active_faces(db, user):
    q = (db.query(GalleryFace)
         .join(GalleryImage, GalleryImage.id == GalleryFace.image_id)
         .filter(GalleryImage.is_active == True, GalleryFace.ignored != True))  # noqa: E712
    return _owner_filter(q, user, GalleryFace)


def _find_person_by_name(db, user, name: str) -> Optional[GalleryPerson]:
    q = db.query(GalleryPerson).filter(func.lower(GalleryPerson.name) == name.lower())
    return _owner_filter(q, user, GalleryPerson).first()


def _clean_name(raw) -> str:
    name = " ".join(str(raw or "").split())[:80]
    if not name:
        raise HTTPException(400, "Enter a name")
    return name


def register_people_routes(router: APIRouter) -> None:
    """Add the people routes (before the /api/gallery/{image_id} catch-all)."""

    def _person_or_404(db, person_id: str, user) -> GalleryPerson:
        q = db.query(GalleryPerson).filter(GalleryPerson.id == person_id)
        person = _owner_filter(q, user, GalleryPerson).first()
        if person is None:
            raise HTTPException(404, "Person not found")
        return person

    def _face_or_404(db, face_id: str, user) -> GalleryFace:
        q = db.query(GalleryFace).filter(GalleryFace.id == face_id)
        face = _owner_filter(q, user, GalleryFace).first()
        if face is None:
            raise HTTPException(404, "Face not found")
        return face

    async def _regroup(user) -> dict:
        return await asyncio.to_thread(cluster_faces, user)

    def _merge(db, source: GalleryPerson, target: GalleryPerson) -> None:
        db.query(GalleryFace).filter(GalleryFace.person_id == source.id).update(
            {GalleryFace.person_id: target.id}, synchronize_session=False)
        if not target.cover_face_id:
            target.cover_face_id = source.cover_face_id
        db.delete(source)

    @router.get("/api/gallery/people")
    async def list_people(request: Request) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            counts = {
                pid: (faces, photos)
                for pid, faces, photos in _active_faces(db, user)
                .filter(GalleryFace.person_id != None)  # noqa: E711
                .with_entities(GalleryFace.person_id, func.count(GalleryFace.id),
                               func.count(func.distinct(GalleryFace.image_id)))
                .group_by(GalleryFace.person_id).all()
            }
            people = []
            for person in _owner_filter(db.query(GalleryPerson), user, GalleryPerson).all():
                faces, photos = counts.get(person.id, (0, 0))
                if not faces:
                    continue
                people.append({
                    "id": person.id, "name": person.name, "face_count": faces, "photo_count": photos,
                    "cover_url": f"/api/gallery/faces/{person.cover_face_id}/crop" if person.cover_face_id else None,
                })
            # most photos first; a tie puts named people (A–Z) before unnamed groups
            people.sort(key=lambda p: (-p["photo_count"], p["name"] is None, (p["name"] or "").lower()))
            unknown = _active_faces(db, user).filter(
                GalleryFace.person_id == None).count()  # noqa: E711
            return {"people": people, "unknown_faces": unknown}
        finally:
            db.close()

    @router.get("/api/gallery/people/{person_id}/faces")
    async def person_faces(request: Request, person_id: str, offset: int = 0, limit: int = 200) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            person = _person_or_404(db, person_id, user)
            faces = _active_faces(db, user).filter(GalleryFace.person_id == person.id)
            total = faces.count()
            photos = faces.with_entities(func.count(func.distinct(GalleryFace.image_id))).scalar() or 0
            # The face id breaks ties so pages never repeat or skip a face.
            rows = (faces.with_entities(GalleryFace, GalleryImage)
                    .order_by(GalleryImage.taken_at.desc(), GalleryFace.id)
                    .offset(max(offset, 0)).limit(max(1, min(limit, 500))).all())
            return {"person": {"id": person.id, "name": person.name},
                    "faces": [_face_dict(f, person, img) for f, img in rows],
                    "total": total, "photo_count": photos}
        finally:
            db.close()

    @router.get("/api/gallery/faces/unknown")
    async def unknown_faces(request: Request, offset: int = 0, limit: int = 200) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            faces = _active_faces(db, user).filter(GalleryFace.person_id == None)  # noqa: E711
            total = faces.count()
            rows = (faces.with_entities(GalleryFace, GalleryImage)
                    .order_by(GalleryFace.score.desc(), GalleryFace.id)
                    .offset(max(offset, 0)).limit(max(1, min(limit, 500))).all())
            return {"faces": [_face_dict(f, None, img) for f, img in rows], "total": total}
        finally:
            db.close()

    @router.patch("/api/gallery/people/{person_id}")
    async def rename_person(request: Request, person_id: str) -> Dict[str, Any]:
        """Name a person. Using a name another person already has merges the two."""
        user = get_current_user(request)
        body = await request.json()
        name = _clean_name(body.get("name"))
        db = SessionLocal()
        try:
            person = _person_or_404(db, person_id, user)
            other = _find_person_by_name(db, user, name)
            merged_into = None
            if other is not None and other.id != person.id:
                _merge(db, person, other)
                merged_into, person = other.id, other
            else:
                person.name = name
            db.commit()
            result = {"ok": True, "person": {"id": person.id, "name": person.name}, "merged_into": merged_into}
        finally:
            db.close()
        result["regrouped"] = await _regroup(user)
        return result

    @router.post("/api/gallery/people/{person_id}/merge")
    async def merge_people(request: Request, person_id: str) -> Dict[str, Any]:
        user = get_current_user(request)
        body = await request.json()
        db = SessionLocal()
        try:
            source = _person_or_404(db, person_id, user)
            target = _person_or_404(db, str(body.get("into") or ""), user)
            if source.id == target.id:
                raise HTTPException(400, "Choose a different person")
            _merge(db, source, target)
            db.commit()
            return {"ok": True, "person_id": target.id}
        finally:
            db.close()

    @router.delete("/api/gallery/people/{person_id}")
    async def dissolve_person(request: Request, person_id: str) -> Dict[str, Any]:
        """Forget a person: their faces go back to unknown and are not regrouped automatically."""
        user = get_current_user(request)
        db = SessionLocal()
        try:
            person = _person_or_404(db, person_id, user)
            n = db.query(GalleryFace).filter(GalleryFace.person_id == person.id).update(
                {GalleryFace.person_id: None, GalleryFace.assigned: "user"}, synchronize_session=False)
            db.delete(person)
            db.commit()
            return {"ok": True, "faces_released": n}
        finally:
            db.close()

    @router.post("/api/gallery/faces/{face_id}")
    async def update_face(request: Request, face_id: str) -> Dict[str, Any]:
        """Correct one face: ``{"name": "Lena"}``, ``{"person_id": "..."}``,
        ``{"person_id": null}`` (not this person) or ``{"ignored": true}`` (not a face)."""
        user = get_current_user(request)
        body = await request.json()
        db = SessionLocal()
        try:
            face = _face_or_404(db, face_id, user)
            if body.get("ignored"):
                face.ignored, face.person_id, face.assigned = True, None, "user"
            elif "name" in body:
                name = _clean_name(body.get("name"))
                person = _find_person_by_name(db, user, name)
                group = db.get(GalleryPerson, face.person_id) if face.person_id else None
                if group is not None and group.name is None:
                    # A face of an unnamed group: the name belongs to the whole group.
                    if person is None:
                        group.name = name
                    else:
                        _merge(db, group, person)
                    face.assigned = "user"
                    db.flush()
                    drop_empty_unnamed_people(db)
                    db.commit()
                    target = person or group
                    result = {"ok": True, "face": _face_dict(face, target, db.get(GalleryImage, face.image_id))}
                    result["face"]["person_id"] = target.id
                    db.close()
                    result["regrouped"] = await _regroup(user)
                    return result
                if person is None:
                    person = GalleryPerson(id=str(uuid.uuid4()), owner=face.owner, name=name, cover_face_id=face.id)
                    db.add(person)
                    db.flush()
                face.person_id, face.assigned, face.ignored = person.id, "user", False
            elif "person_id" in body:
                pid = body.get("person_id")
                if pid:
                    face.person_id = _person_or_404(db, str(pid), user).id
                else:
                    face.person_id = None
                face.assigned, face.ignored = "user", False
            else:
                raise HTTPException(400, "Nothing to change")
            db.flush()
            drop_empty_unnamed_people(db)
            db.commit()
            person = db.get(GalleryPerson, face.person_id) if face.person_id else None
            img = db.get(GalleryImage, face.image_id)
            result = {"ok": True, "face": _face_dict(face, person, img)}
        finally:
            db.close()
        result["regrouped"] = await _regroup(user)
        return result

    @router.post("/api/gallery/people/regroup")
    async def regroup(request: Request) -> Dict[str, Any]:
        return {"ok": True, **(await _regroup(get_current_user(request)))}

    @router.get("/api/gallery/faces/{face_id}/crop")
    async def face_crop(request: Request, face_id: str):
        user = get_current_user(request)
        db = SessionLocal()
        try:
            face = _face_or_404(db, face_id, user)
        finally:
            db.close()
        path = crop_path(face.id)
        if not os.path.isfile(path):
            raise HTTPException(404, "Face image not available")
        return FileResponse(path, media_type="image/jpeg", headers=FILE_HEADERS)

    @router.get("/api/gallery/{image_id}/faces")
    async def image_faces(request: Request, image_id: str) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            img = _owner_filter(db.query(GalleryImage).filter(GalleryImage.id == image_id), user).first()
            if img is None:
                raise HTTPException(404, "Image not found")
            rows = (db.query(GalleryFace, GalleryPerson)
                    .outerjoin(GalleryPerson, GalleryPerson.id == GalleryFace.person_id)
                    .filter(GalleryFace.image_id == img.id, GalleryFace.ignored != True)  # noqa: E712
                    .order_by(GalleryFace.box_x).all())
            return {"faces": [_face_dict(f, p, img) for f, p in rows], "scanned": bool(img.faces_scanned)}
        finally:
            db.close()

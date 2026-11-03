"""Local image servers that Odysseus can start and stop.

A server run by scripts/image_server_supervisor.py answers /manager/* on its
endpoint's address. Image requests already start it by themselves; these routes
show its state in Settings and let an admin start it, stop it and choose when
it stops on its own. Only local endpoints are asked, never a cloud provider.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Request

from core.database import ModelEndpoint, SessionLocal
from core.middleware import require_admin
from src.auth_helpers import get_current_user, owner_filter
from src.model_context import is_local_endpoint

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 3
_HELPER_DOWN = "The image server helper is not running on this computer."


def _manager_url(base_url: str, name: str) -> str:
    root = (base_url or "").rstrip("/")
    return f"{root[:-3] if root.endswith('/v1') else root}/manager/{name}"


def _local_image_endpoints(owner: Optional[str]) -> List[Tuple[str, str, str]]:
    """``(id, name, base_url)`` of the enabled image endpoints on this machine or network."""
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(
            ModelEndpoint.is_enabled == True,  # noqa: E712
            ModelEndpoint.model_type == "image",
        )
        if owner:
            q = owner_filter(q, ModelEndpoint, owner)
        return [(e.id, e.name, e.base_url) for e in q.all() if is_local_endpoint(e.base_url)]
    finally:
        db.close()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, trust_env=False)


async def _status(base_url: str) -> Optional[Dict[str, Any]]:
    """The server's state, or None if nothing managed answers there."""
    try:
        async with _client() as client:
            response = await client.get(_manager_url(base_url, "status"))
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def _command(base_url: str, name: str, body: Optional[dict] = None) -> Dict[str, Any]:
    try:
        async with _client() as client:
            response = await client.post(_manager_url(base_url, name), json=body or {})
    except httpx.HTTPError as exc:
        logger.warning("Image server helper at %s did not answer: %s", base_url, exc)
        raise HTTPException(502, _HELPER_DOWN) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200:
        message = (payload.get("error") or {}).get("message") if isinstance(payload, dict) else None
        raise HTTPException(400 if response.status_code == 400 else 502, message or _HELPER_DOWN)
    return payload


def _endpoint(endpoint_id: str, owner: Optional[str]) -> str:
    for known_id, _name, base_url in _local_image_endpoints(owner):
        if known_id == endpoint_id:
            return base_url
    raise HTTPException(404, "Image server not found")


def setup_image_server_routes() -> APIRouter:
    router = APIRouter(tags=["image-servers"])

    @router.get("/api/image-servers")
    async def list_image_servers(request: Request) -> Dict[str, Any]:
        require_admin(request)
        endpoints = _local_image_endpoints(get_current_user(request))
        states = await asyncio.gather(*(_status(base_url) for _id, _name, base_url in endpoints))
        return {"servers": [
            {**state, "id": endpoint_id, "name": name}
            for (endpoint_id, name, _base), state in zip(endpoints, states) if state
        ]}

    @router.post("/api/image-servers/{endpoint_id}/start")
    async def start_image_server(request: Request, endpoint_id: str) -> Dict[str, Any]:
        require_admin(request)
        return await _command(_endpoint(endpoint_id, get_current_user(request)), "start")

    @router.post("/api/image-servers/{endpoint_id}/stop")
    async def stop_image_server(request: Request, endpoint_id: str) -> Dict[str, Any]:
        require_admin(request)
        return await _command(_endpoint(endpoint_id, get_current_user(request)), "stop")

    @router.post("/api/image-servers/{endpoint_id}/settings")
    async def image_server_settings(request: Request, endpoint_id: str) -> Dict[str, Any]:
        require_admin(request)
        base_url = _endpoint(endpoint_id, get_current_user(request))
        body = await request.json()
        return await _command(base_url, "settings", {"idle_minutes": body.get("idle_minutes")})

    return router

"""Settings controls for local image servers that run under the supervisor."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

import routes.image_server_routes as routes
from core.database import ModelEndpoint
from tests.helpers.database import disposable_database

MANAGED = "http://host.docker.internal:8101/v1"
PLAIN = "http://host.docker.internal:8200/v1"
CLOUD = "https://api.example.com/v1"


class FakeRequest(SimpleNamespace):
    async def json(self):
        return self.body


def _route(router, path, method):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"no route {method} {path}")


@pytest.fixture
def api(tmp_path, monkeypatch):
    """The routes against a database with three endpoints and a fake helper."""
    seen = []

    def helper(request):
        seen.append((request.method, str(request.url), request.content))
        if request.url.host == "api.example.com" or request.url.port == 8200:
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.url.path.endswith("/settings") and json.loads(request.content)["idle_minutes"] == -1:
            return httpx.Response(400, json={"error": {"message": "idle_minutes must be 0 to 1440"}})
        return httpx.Response(200, json={"state": "stopped", "idle_minutes": 10, "model": "flux"})

    with disposable_database(tmp_path) as factory:
        db = factory()
        db.add_all([
            ModelEndpoint(id="managed", name="FLUX", base_url=MANAGED, model_type="image", is_enabled=True),
            ModelEndpoint(id="plain", name="Other", base_url=PLAIN, model_type="image", is_enabled=True),
            ModelEndpoint(id="cloud", name="Cloud", base_url=CLOUD, model_type="image", is_enabled=True),
            ModelEndpoint(id="off", name="Off", base_url=MANAGED, model_type="image", is_enabled=False),
            ModelEndpoint(id="llm", name="Chat", base_url=MANAGED, model_type="llm", is_enabled=True),
        ])
        db.commit()
        db.close()
        monkeypatch.setattr(routes, "SessionLocal", factory)
        monkeypatch.setattr(routes, "require_admin", lambda request: None)
        monkeypatch.setattr(routes, "get_current_user", lambda request: None)
        monkeypatch.setattr(routes, "is_local_endpoint", lambda url: "example.com" not in url)
        monkeypatch.setattr(routes, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(helper)))
        yield SimpleNamespace(router=routes.setup_image_server_routes(), seen=seen)


def test_only_supervised_local_image_servers_are_listed(api):
    result = asyncio.run(_route(api.router, "/api/image-servers", "GET")(FakeRequest()))

    assert [(s["id"], s["name"], s["state"]) for s in result["servers"]] == [("managed", "FLUX", "stopped")]


def test_cloud_providers_are_never_asked(api):
    asyncio.run(_route(api.router, "/api/image-servers", "GET")(FakeRequest()))

    assert sorted(url for _method, url, _body in api.seen) == [
        "http://host.docker.internal:8101/manager/status",
        "http://host.docker.internal:8200/manager/status",
    ]


@pytest.mark.parametrize("action", ["start", "stop"])
def test_start_and_stop_reach_the_helper(api, action):
    handler = _route(api.router, f"/api/image-servers/{{endpoint_id}}/{action}", "POST")

    assert asyncio.run(handler(FakeRequest(), "managed"))["state"] == "stopped"
    assert api.seen[-1][:2] == ("POST", f"http://host.docker.internal:8101/manager/{action}")


@pytest.mark.parametrize("endpoint_id", ["missing", "cloud", "off", "llm"])
def test_other_endpoints_cannot_be_controlled(api, endpoint_id):
    handler = _route(api.router, "/api/image-servers/{endpoint_id}/start", "POST")

    with pytest.raises(HTTPException) as err:
        asyncio.run(handler(FakeRequest(), endpoint_id))

    assert err.value.status_code == 404
    assert api.seen == []


def test_the_idle_time_is_passed_on_and_a_bad_value_explained(api):
    handler = _route(api.router, "/api/image-servers/{endpoint_id}/settings", "POST")

    asyncio.run(handler(FakeRequest(body={"idle_minutes": 30, "other": 1}), "managed"))
    assert json.loads(api.seen[-1][2]) == {"idle_minutes": 30}

    with pytest.raises(HTTPException) as err:
        asyncio.run(handler(FakeRequest(body={"idle_minutes": -1}), "managed"))
    assert (err.value.status_code, err.value.detail) == (400, "idle_minutes must be 0 to 1440")


def test_a_stopped_helper_gives_a_clear_message(api, monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(routes, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(down)))
    handler = _route(api.router, "/api/image-servers/{endpoint_id}/start", "POST")

    with pytest.raises(HTTPException) as err:
        asyncio.run(handler(FakeRequest(), "managed"))

    assert err.value.status_code == 502
    assert "not running on this computer" in err.value.detail


def test_the_routes_are_for_admins(api, monkeypatch):
    def refuse(request):
        raise HTTPException(403, "Admin required")

    monkeypatch.setattr(routes, "require_admin", refuse)

    with pytest.raises(HTTPException) as err:
        asyncio.run(_route(api.router, "/api/image-servers", "GET")(FakeRequest()))

    assert err.value.status_code == 403
    assert api.seen == []

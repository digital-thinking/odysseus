"""The helper that starts a local image server on demand and stops it when idle."""

import http.client
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "image_server_supervisor.py"
_spec = importlib.util.spec_from_file_location("image_server_supervisor", _SCRIPT)
supervisor = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = supervisor
_spec.loader.exec_module(supervisor)

# Stands in for the diffusion server: loads for a while, then answers.
FAKE_SERVER = r'''
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

port, load_seconds = int(sys.argv[1]), float(sys.argv[2])
time.sleep(load_seconds)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.reply({"status": "ok", "path": self.path})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.path == "/slow":
            time.sleep(1.5)
        self.reply({"pid": os.getpid(), "path": self.path, "body": body, "auth": self.headers.get("Authorization")})

    def reply(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Harness:
    def __init__(self, tmp_path, *, load_seconds=0.0, idle_minutes=10, cmd=None, start_timeout=20):
        script = tmp_path / "fake_server.py"
        script.write_text(FAKE_SERVER, encoding="utf-8")
        self.backend_port = _free_port()
        self.cmd = cmd or [sys.executable, str(script), str(self.backend_port), str(load_seconds)]
        self.backend = supervisor.Backend(self.cmd, self.backend_port, tmp_path, idle_minutes=idle_minutes,
                                          start_timeout=start_timeout, watch_seconds=0.05)
        self.server = supervisor.make_server(self.backend, "test-model")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            reply = conn.getresponse()
            data = reply.read()
            return reply.status, json.loads(data)
        finally:
            conn.close()

    def wait_for(self, state, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.backend.state == state:
                return
            time.sleep(0.02)
        raise AssertionError(f"state stayed {self.backend.state}, expected {state}")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.backend.close()


@pytest.fixture
def harness(tmp_path):
    made = []

    def make(**kwargs):
        made.append(Harness(tmp_path, **kwargs))
        return made[-1]

    yield make
    for item in made:
        item.close()


def test_endpoint_checks_do_not_load_the_model(harness):
    h = harness()

    assert h.request("GET", "/v1/models") == (
        200, {"data": [{"id": "test-model", "object": "model", "owned_by": "local"}]})
    assert h.request("GET", "/health")[1]["state"] == "stopped"
    assert h.request("GET", "/manager/status")[1]["state"] == "stopped"

    assert h.backend.state == "stopped"


def test_the_first_image_request_starts_the_server_and_later_ones_reuse_it(harness):
    h = harness(load_seconds=0.2)

    status, first = h.request("POST", "/v1/images/generations", body='{"prompt": "a cat"}',
                              headers={"Authorization": "Bearer abc", "Content-Type": "application/json"})
    assert status == 200
    assert first["path"] == "/v1/images/generations"
    assert first["body"] == '{"prompt": "a cat"}'
    assert first["auth"] == "Bearer abc"
    assert h.backend.state == "running"

    _, second = h.request("POST", "/v1/images/generations", body="{}")
    assert second["pid"] == first["pid"]


def test_a_chunked_upload_reaches_the_server_whole(harness):
    h = harness()

    conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=30)
    conn.request("POST", "/v1/images/edits", body=iter([b"part one ", b"part two"]),
                 headers={"Transfer-Encoding": "chunked"}, encode_chunked=True)
    reply = json.loads(conn.getresponse().read())
    conn.close()

    assert reply["body"] == "part one part two"


def test_progress_polling_keeps_working_during_a_cold_start_without_starting_anything(harness):
    h = harness(load_seconds=1)

    # A 404 here would end Odysseus's progress polling.
    assert h.request("GET", "/v1/images/progress/abc") == (
        200, {"id": "abc", "status": "unknown", "step": 0, "total": 0, "percent": 0})
    assert h.backend.state == "stopped"

    h.backend.start()
    assert h.backend.state == "starting"
    assert h.request("GET", "/v1/images/progress/abc")[1]["status"] == "unknown"

    h.wait_for("running")
    assert h.request("GET", "/v1/images/progress/abc")[1]["path"] == "/v1/images/progress/abc"


def test_the_server_stops_after_the_idle_time_and_starts_again_on_demand(harness):
    h = harness(idle_minutes=0.5 / 60)

    _, first = h.request("POST", "/v1/images/generations", body="{}")
    h.wait_for("stopped")
    _, second = h.request("POST", "/v1/images/generations", body="{}")

    assert second["pid"] != first["pid"]


def test_a_running_image_is_never_stopped_for_being_idle(harness):
    h = harness(idle_minutes=0.5 / 60)
    h.request("POST", "/v1/images/generations", body="{}")   # server up

    status, reply = h.request("POST", "/slow", body="{}")    # takes longer than the idle time

    assert status == 200 and reply["path"] == "/slow"


def test_idle_zero_means_it_keeps_running(harness):
    h = harness(idle_minutes=0)
    h.request("POST", "/v1/images/generations", body="{}")

    time.sleep(0.3)

    assert h.backend.state == "running"
    assert h.request("GET", "/manager/status")[1]["stops_in_seconds"] is None


def test_start_and_stop_from_the_manager(harness):
    h = harness()

    assert h.request("POST", "/manager/start")[0] == 200
    h.wait_for("running")
    status = h.request("GET", "/manager/status")[1]
    assert status["model"] == "test-model"
    assert status["stops_in_seconds"] > 0

    assert h.request("POST", "/manager/stop")[0] == 200
    h.wait_for("stopped")
    assert h.backend.last_error is None


def test_a_server_that_cannot_start_reports_why(harness, tmp_path):
    h = harness(cmd=[sys.executable, "-c", "print('boom'); raise SystemExit(3)"])

    status, reply = h.request("POST", "/v1/images/generations", body="{}")

    assert status == 503
    assert "code 3" in reply["error"]["message"]
    assert h.request("GET", "/manager/status")[1]["last_error"]
    assert "boom" in (tmp_path / "server.log").read_text(encoding="utf-8")


def test_a_missing_program_is_reported_not_raised(harness):
    h = harness(cmd=["/no/such/program"])

    status, reply = h.request("POST", "/v1/images/generations", body="{}")

    assert status == 503
    assert "Could not start" in reply["error"]["message"]


def test_a_crash_while_running_goes_back_to_stopped(harness):
    h = harness()
    h.request("POST", "/v1/images/generations", body="{}")

    h.backend._proc.kill()
    h.wait_for("stopped")

    assert "exited" in h.backend.last_error
    assert h.request("POST", "/v1/images/generations", body="{}")[0] == 200   # and recovers


def test_the_idle_time_is_validated_and_survives_a_restart(harness, tmp_path):
    h = harness()

    assert h.request("POST", "/manager/settings", body='{"idle_minutes": "soon"}')[0] == 400
    assert h.request("POST", "/manager/settings", body='{"idle_minutes": -1}')[0] == 400
    assert h.request("POST", "/manager/settings", body='{"idle_minutes": 30}')[1]["idle_minutes"] == 30

    assert supervisor.Backend(["unused"], 1, tmp_path, idle_minutes=10).idle_minutes == 30


def test_only_odysseus_can_reach_it(harness):
    h = harness()

    assert h.request("GET", "/v1/models", headers={"Host": "evil.example"})[0] == 403
    assert h.request("POST", "/manager/stop", headers={"Origin": "https://evil.example"})[0] == 403
    assert h.request("GET", "/manager/status", headers={"Host": "host.docker.internal:8101"})[0] == 200

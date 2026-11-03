#!/usr/bin/env python3
"""Run a local image server only while it is needed.

A diffusion model holds many GB of memory, so it should not stay loaded between
images, and Odysseus in Docker cannot start a program on the host by itself.
This small process (standard library only) stays running on the host instead:

* it listens on the port Odysseus knows and forwards image requests to the real
  server, starting that server on the first request;
* it answers /v1/models and /health itself, so Odysseus's endpoint checks keep
  the model listed without loading it;
* it stops the server after --idle-minutes without an image request;
* /manager/* lets Odysseus show the state and start or stop the server.

    python image_server_supervisor.py --port 8101 --backend-port 8102 \\
        --model-id FLUX.2-klein-9B --state-dir DIR -- python serve_flux.py
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

logger = logging.getLogger("image-server-supervisor")

STOPPED, STARTING, RUNNING, STOPPING = "stopped", "starting", "running", "stopping"

MAX_IDLE_MINUTES = 24 * 60
_FORWARD_TIMEOUT_SECONDS = 900
_READY_POLL_SECONDS = 1
_STOP_WAIT_SECONDS = 20
_SETTINGS_FILE = "supervisor.json"
_SERVER_LOG = "server.log"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "host.docker.internal"}
# Not passed on: they describe this connection, not the request.
_HOP_BY_HOP = {"connection", "content-length", "host", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}


class BackendUnavailable(RuntimeError):
    """The image server could not be started."""


class _WindowsJob:
    """Holds the server and its child processes in one Windows job object.

    One call stops them all, and Windows stops them if this process dies, so a
    crashed supervisor cannot leave many GB of model behind.
    """

    _CREATE_SUSPENDED = 0x00000004
    _CREATE_NO_WINDOW = 0x08000000
    _JOB_EXTENDED_LIMITS = 9
    _KILL_ON_JOB_CLOSE = 0x00002000

    creationflags = _CREATE_SUSPENDED | _CREATE_NO_WINDOW

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll")
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                           wintypes.DWORD]
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

        self._job = self._kernel32.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(self._job, self._JOB_EXTENDED_LIMITS,
                                                      ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())

    def adopt(self, proc: subprocess.Popen) -> None:
        """Take over a suspended process, then let it run (so no child can escape the job)."""
        import ctypes

        handle = int(proc._handle)  # noqa: SLF001 - Popen has no public handle
        if not self._kernel32.AssignProcessToJobObject(self._job, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self._ntdll.NtResumeProcess(handle)

    def terminate(self) -> None:
        self._kernel32.TerminateJobObject(self._job, 1)

    def close(self) -> None:
        self._kernel32.CloseHandle(self._job)


class Backend:
    """The real image server: start on demand, stop when idle."""

    def __init__(self, cmd: list[str], port: int, state_dir: Path, *, idle_minutes: float = 10,
                 start_timeout: float = 600, watch_seconds: float = 2):
        self.cmd = cmd
        self.port = port
        self.state_dir = Path(state_dir)
        self.start_timeout = start_timeout
        self.watch_seconds = watch_seconds
        self.idle_minutes = self._saved_idle_minutes(idle_minutes)
        self.state = STOPPED
        self.last_error: Optional[str] = None
        self.in_flight = 0
        self._last_used = time.monotonic()
        self._started_at: Optional[float] = None
        self._proc: Optional[subprocess.Popen] = None
        self._job: Optional[_WindowsJob] = None
        self._cond = threading.Condition()
        self._closing = threading.Event()
        threading.Thread(target=self._watch, daemon=True, name="image-server-watch").start()

    def _settings_path(self) -> Path:
        return self.state_dir / _SETTINGS_FILE

    def _saved_idle_minutes(self, default: float) -> float:
        try:
            return float(json.loads(self._settings_path().read_text(encoding="utf-8"))["idle_minutes"])
        except (OSError, ValueError, KeyError, TypeError):
            return default

    def set_idle_minutes(self, minutes: float) -> None:
        with self._cond:
            self.idle_minutes = minutes
            self._last_used = time.monotonic()
        self._settings_path().write_text(json.dumps({"idle_minutes": minutes}), encoding="utf-8")

    def status(self) -> dict:
        with self._cond:
            idle_left = None
            if self.state == RUNNING and self.idle_minutes > 0 and not self.in_flight:
                idle_left = max(0, round(self.idle_minutes * 60 - (time.monotonic() - self._last_used)))
            return {
                "state": self.state,
                "idle_minutes": self.idle_minutes,
                "stops_in_seconds": idle_left,
                "in_flight": self.in_flight,
                "running_seconds": round(time.monotonic() - self._started_at) if self._started_at else None,
                "last_error": self.last_error,
            }

    def start(self) -> None:
        """Begin starting the server if it is stopped; returns at once."""
        with self._cond:
            if self.state == STOPPED:
                self._launch()

    def claim(self) -> None:
        """Wait until the server runs and count one request against it; pair with :meth:`release`."""
        deadline = time.monotonic() + self.start_timeout
        attempted = False
        with self._cond:
            while True:
                if self.state == RUNNING:
                    self.in_flight += 1
                    return
                if self.state == STOPPED:
                    if attempted:
                        raise BackendUnavailable(self.last_error or "The image server stopped while starting")
                    attempted = True
                    self._launch()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendUnavailable("The image server is still starting; try again in a moment")
                self._cond.wait(remaining)

    def release(self) -> None:
        with self._cond:
            self.in_flight = max(0, self.in_flight - 1)
            self._last_used = time.monotonic()

    def _launch(self) -> None:
        """Start the process; the caller holds the lock."""
        job = None
        self.last_error = None
        try:
            log = open(self.state_dir / _SERVER_LOG, "wb")
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
            try:
                if os.name == "nt":
                    job = _WindowsJob()
                    proc = subprocess.Popen(self.cmd, creationflags=job.creationflags, **kwargs)
                    job.adopt(proc)
                else:
                    proc = subprocess.Popen(self.cmd, start_new_session=True, **kwargs)
            finally:
                log.close()
        except OSError as exc:
            if job is not None:
                job.close()
            self.last_error = f"Could not start the image server: {exc}"
            logger.error(self.last_error)
            self._cond.notify_all()
            return
        self._proc, self._job = proc, job
        self.state = STARTING
        self._started_at = None
        logger.info("Starting image server (pid %s)", proc.pid)
        self._cond.notify_all()
        threading.Thread(target=self._await_ready, args=(proc,), daemon=True, name="image-server-ready").start()

    def _healthy(self) -> bool:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("GET", "/health")
            return conn.getresponse().status == 200
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def _await_ready(self, proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + self.start_timeout
        while proc.poll() is None:
            if self._healthy():
                with self._cond:
                    if self._proc is proc and self.state == STARTING:
                        self.state = RUNNING
                        self._started_at = self._last_used = time.monotonic()
                        logger.info("Image server is ready")
                        self._cond.notify_all()
                return
            if time.monotonic() > deadline:
                self.stop(error=f"The image server was not ready after {round(self.start_timeout)} seconds")
                return
            time.sleep(_READY_POLL_SECONDS)
        self._exited(proc)

    def _exited(self, proc: subprocess.Popen) -> None:
        """The server ended by itself."""
        with self._cond:
            if self._proc is not proc or self.state == STOPPING:
                return
            message = f"The image server exited (code {proc.returncode}); see {self.state_dir / _SERVER_LOG}"
            logger.error(message)
            self._clear(error=message)

    def _clear(self, error: Optional[str] = None) -> None:
        """Back to stopped; the caller holds the lock."""
        if self._job is not None:
            self._job.close()
        self._proc = self._job = self._started_at = None
        self.state = STOPPED
        self.last_error = error
        self._cond.notify_all()

    def stop(self, error: Optional[str] = None) -> None:
        with self._cond:
            proc, job = self._proc, self._job
            if proc is None or self.state == STOPPING:
                return
            self.state = STOPPING
            self._cond.notify_all()
        logger.info("Stopping image server (pid %s)", proc.pid)
        if job is not None:
            job.terminate()
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            proc.wait(_STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            if job is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(_STOP_WAIT_SECONDS)
        with self._cond:
            self._clear(error=error)

    def _watch(self) -> None:
        while not self._closing.wait(self.watch_seconds):
            with self._cond:
                proc, state = self._proc, self.state
                idle = time.monotonic() - self._last_used
                due = (state == RUNNING and not self.in_flight and 0 < self.idle_minutes
                       and idle >= self.idle_minutes * 60)
            if state == RUNNING and proc is not None and proc.poll() is not None:
                self._exited(proc)
            elif due:
                logger.info("Idle for %d seconds", idle)
                self.stop()

    def close(self) -> None:
        self._closing.set()
        self.stop()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second copy bind the same port and
    # steal requests; failing to start is the right outcome there.
    allow_reuse_address = os.name != "nt"


def _handler(backend: Backend, model_id: str, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "image-server-supervisor"

        def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler's name
            logger.debug("%s %s", self.address_string(), format % args)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                chunks = []
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        while self.rfile.readline().strip():
                            pass  # trailers
                        return b"".join(chunks)
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _route(self) -> None:
            body = self._body()
            path = self.path.split("?", 1)[0]
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            if host not in allowed_hosts:   # DNS rebinding
                self._json(403, {"error": {"message": "Host not allowed"}})
            elif path.startswith("/manager/"):
                self._manager(path, body)
            elif path == "/v1/models":
                self._json(200, {"data": [{"id": model_id, "object": "model", "owned_by": "local"}]})
            elif path == "/health":
                self._json(200, {"status": "ok", "model": model_id, "state": backend.state})
            elif path.startswith("/v1/images/progress/"):
                self._progress(path, body)
            else:
                self._image_request(body)

        def _manager(self, path: str, body: bytes) -> None:
            if self.headers.get("Origin"):   # only Odysseus's server calls this, never a web page
                self._json(403, {"error": {"message": "Not available from a browser"}})
            elif path == "/manager/status" and self.command == "GET":
                self._json(200, {**backend.status(), "model": model_id})
            elif path == "/manager/start" and self.command == "POST":
                backend.start()
                self._json(200, {**backend.status(), "model": model_id})
            elif path == "/manager/stop" and self.command == "POST":
                threading.Thread(target=backend.stop, daemon=True, name="image-server-stop").start()
                self._json(200, {**backend.status(), "model": model_id})
            elif path == "/manager/settings" and self.command == "POST":
                try:
                    minutes = float(json.loads(body)["idle_minutes"])
                except (ValueError, KeyError, TypeError):
                    self._json(400, {"error": {"message": "idle_minutes must be a number"}})
                    return
                if not 0 <= minutes <= MAX_IDLE_MINUTES:
                    self._json(400, {"error": {"message": f"idle_minutes must be 0 to {MAX_IDLE_MINUTES}"}})
                    return
                backend.set_idle_minutes(minutes)
                self._json(200, {**backend.status(), "model": model_id})
            else:
                self._json(404, {"error": {"message": "Not found"}})

        def _progress(self, path: str, body: bytes) -> None:
            """Never wakes the server, and answers like it does: a 404 ends Odysseus's polling."""
            if backend.state == RUNNING:
                self._forward(body)
            else:
                self._json(200, {"id": path.rsplit("/", 1)[-1], "status": "unknown",
                                 "step": 0, "total": 0, "percent": 0})

        def _image_request(self, body: bytes) -> None:
            try:
                backend.claim()
            except BackendUnavailable as exc:
                self._json(503, {"error": {"message": str(exc)}})
                return
            try:
                self._forward(body)
            finally:
                backend.release()

        def _forward(self, body: bytes) -> None:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
            conn = http.client.HTTPConnection("127.0.0.1", backend.port, timeout=_FORWARD_TIMEOUT_SECONDS)
            try:
                conn.request(self.command, self.path, body=body or None, headers=headers)
                reply = conn.getresponse()
                content = reply.read()
            except (OSError, http.client.HTTPException) as exc:
                self._json(502, {"error": {"message": f"The image server did not answer: {exc}"}})
                return
            finally:
                conn.close()
            self.send_response(reply.status)
            for key, value in reply.getheaders():
                if key.lower() not in _HOP_BY_HOP and key.lower() not in ("date", "server"):
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _route

    return Handler


def make_server(backend: Backend, model_id: str, host: str = "127.0.0.1", port: int = 0,
                allowed_hosts: Optional[set[str]] = None) -> ThreadingHTTPServer:
    return _Server((host, port), _handler(backend, model_id, _LOCAL_HOSTS | (allowed_hosts or set())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, required=True, help="Port Odysseus connects to")
    parser.add_argument("--backend-port", type=int, required=True, help="Port the image server listens on")
    parser.add_argument("--model-id", required=True, help="Model name shown in Odysseus")
    parser.add_argument("--state-dir", type=Path, required=True, help="Folder for logs and settings")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--idle-minutes", type=float, default=10, help="Stop after this long unused; 0 = never")
    parser.add_argument("--start-timeout", type=float, default=600, help="Seconds the server may take to load")
    parser.add_argument("--allowed-host", action="append", default=[], help="Extra allowed Host header")
    parser.add_argument("command", nargs="+", help="Command that runs the image server (after --)")
    args = parser.parse_args()

    args.state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.state_dir / "supervisor.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    backend = Backend(args.command, args.backend_port, args.state_dir, idle_minutes=args.idle_minutes,
                      start_timeout=args.start_timeout)
    try:
        server = make_server(backend, args.model_id, args.host, args.port, set(args.allowed_host))
    except OSError:
        logger.exception("Could not listen on port %s", args.port)
        raise
    logger.info("Listening on %s:%s for %s (idle stop: %s min)", args.host, args.port, args.model_id,
                backend.idle_minutes)
    try:
        server.serve_forever()
    finally:
        backend.close()


if __name__ == "__main__":
    main()

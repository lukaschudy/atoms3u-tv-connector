"""Loopback-only visual status canvas for the Raspberry Pi HDMI display."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_state import AgentState, StateEvent
from protocol import ActionRequest


MAX_DISPLAY_TEXT = 280
_TIMED_STATES = {
    AgentState.LISTENING,
    AgentState.TRANSCRIBING,
    AgentState.ROUTING,
    AgentState.EXECUTING,
}
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def _display_text(value: object) -> str:
    return " ".join(str(value).split())[:MAX_DISPLAY_TEXT]


class RoomCanvasState:
    """Thread-safe display model fed by the existing agent lifecycle."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        now = self._clock()
        self._state: dict[str, Any] = {
            "version": 1,
            "sequence": 0,
            "state": AgentState.STARTING.value,
            "detail": "Starting local services",
            "transcript": "",
            "action": "",
            "target": "",
            "result": {},
            "stage_ms": {},
            "total_ms": None,
            "command_started_at": None,
            "updated_at": now,
        }
        self._last_event_at: float | None = None
        self._command_started_at: float | None = None

    def start_command(self) -> None:
        now = self._clock()
        with self._lock:
            self._command_started_at = now
            self._state.update(
                {
                    "transcript": "",
                    "action": "",
                    "target": "",
                    "result": {},
                    "stage_ms": {},
                    "total_ms": None,
                    "command_started_at": now,
                    "updated_at": now,
                }
            )

    def observe_state(self, event: StateEvent) -> None:
        with self._lock:
            if (
                self._command_started_at is not None
                and self._last_event_at is not None
                and event.previous in _TIMED_STATES
            ):
                elapsed_ms = max(0, round((event.occurred_at - self._last_event_at) * 1000))
                self._state["stage_ms"][event.previous.value.lower()] = elapsed_ms

            self._last_event_at = event.occurred_at
            self._state.update(
                {
                    "sequence": event.sequence,
                    "state": event.state.value,
                    "detail": _display_text(event.detail),
                    "updated_at": event.occurred_at,
                }
            )

            if event.state in {AgentState.SUCCESS, AgentState.ERROR}:
                if self._command_started_at is not None:
                    self._state["total_ms"] = max(
                        0,
                        round((event.occurred_at - self._command_started_at) * 1000),
                    )
                if event.state == AgentState.ERROR and event.detail:
                    self._state["result"] = {"message": _display_text(event.detail)}

    def record_transcript(self, transcript: str) -> None:
        with self._lock:
            self._state["transcript"] = _display_text(transcript)
            self._state["updated_at"] = self._clock()

    def record_action(self, action: ActionRequest) -> None:
        with self._lock:
            self._state["action"] = _display_text(action.action)
            self._state["target"] = _display_text(action.target)
            self._state["updated_at"] = self._clock()

    def record_result(self, response: Mapping[str, object]) -> None:
        visible_fields = (
            "status",
            "title",
            "channel_title",
            "query",
            "provider",
            "media_status",
        )
        result = {
            name: _display_text(response[name])
            for name in visible_fields
            if response.get(name) not in (None, "")
        }
        with self._lock:
            self._state["result"] = result
            self._state["updated_at"] = self._clock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))


class _CanvasHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address,
        handler_class,
        *,
        state: RoomCanvasState,
        asset_dir: Path,
    ) -> None:
        self.canvas_state = state
        self.asset_dir = asset_dir
        super().__init__(server_address, handler_class)


class _CanvasHandler(BaseHTTPRequestHandler):
    server: _CanvasHttpServer

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/state":
            body = json.dumps(
                self.server.canvas_state.snapshot(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self._respond(body, "application/json; charset=utf-8")
            return
        if path == "/health":
            self._respond(b'{"status":"ok"}', "application/json; charset=utf-8")
            return
        asset = _ASSETS.get(path)
        if asset is None:
            self.send_error(404)
            return
        filename, content_type = asset
        try:
            body = (self.server.asset_dir / filename).read_bytes()
        except OSError:
            self.send_error(500)
            return
        self._respond(body, content_type)

    def _respond(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class RoomCanvasServer:
    """Serve the visual canvas on loopback from a thread in the hub process."""

    def __init__(
        self,
        *,
        port: int = 8768,
        asset_dir: Path | None = None,
        state: RoomCanvasState | None = None,
    ) -> None:
        self.port = port
        self.asset_dir = asset_dir or Path(__file__).with_name("room_canvas")
        self.state = state or RoomCanvasState()
        self._server: _CanvasHttpServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("Room Canvas has not been started.")
        return f"http://127.0.0.1:{self._server.server_port}/"

    def start(self) -> str:
        if self._server is not None:
            return self.url
        missing = [name for name, _type in _ASSETS.values() if not (self.asset_dir / name).is_file()]
        if missing:
            raise RuntimeError(f"Room Canvas assets are missing: {', '.join(missing)}")
        self._server = _CanvasHttpServer(
            ("127.0.0.1", self.port),
            _CanvasHandler,
            state=self.state,
            asset_dir=self.asset_dir,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="room-canvas",
            daemon=True,
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)

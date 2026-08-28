"""Virtual ATOMS3U USB-TV connector for development before hardware arrives."""

from __future__ import annotations

import argparse
import json
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from protocol import (
    ProtocolError,
    consume_verified_nonce,
    read_key,
    sign_response,
    verify_request_authentication,
)
from tv_connector import (
    CONNECTOR_KEYS_BY_PROFILE,
    CONNECTOR_VERSION,
    DEFAULT_CONNECTOR_PROFILE,
    MAX_CONNECTOR_BODY_BYTES,
    RESPONSE_AUTH_CAPABILITY,
    SUPPORTED_CONNECTOR_PROFILES,
    ConnectorPlan,
    TvConnectorError,
)


@dataclass
class VirtualTvState:
    boot_id: str = field(default_factory=lambda: secrets.token_hex(16))
    screen: str = "home"
    active_app: str | None = None
    query: str | None = None
    media: str = "stopped"
    muted: bool = False
    volume: int = 20
    typed_text: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def execute(self, plan: ConnectorPlan) -> dict[str, Any]:
        for step in plan.steps:
            if step.op == "text":
                self.typed_text = str(step.value)
            elif step.op == "key":
                self._key(str(step.value))

        if plan.purpose == "app.open":
            self.active_app = self.typed_text
            self.screen = "app"
        elif plan.purpose == "web.search":
            self.query = self.typed_text
            self.screen = "search_results"
        elif plan.purpose == "web.open_first":
            self.screen = "first_result"
        elif plan.purpose == "youtube.play":
            self.active_app = "YouTube"
            self.query = self.typed_text
            self.screen = "app"
            self.media = "playing"
        elif plan.purpose == "media.pause":
            self.media = "paused"
        elif plan.purpose == "media.resume":
            self.media = "playing"
        elif plan.purpose == "media.close":
            self.screen = "home"
            self.active_app = None
            self.media = "stopped"

        event = {
            "request_id": plan.request_id,
            "purpose": plan.purpose,
            "profile": plan.profile,
            "step_count": len(plan.steps),
        }
        if plan.purpose != "connector.verify":
            self.history.append(event)
        return self.snapshot()

    def _key(self, key: str) -> None:
        if key == "HOME":
            self.screen = "home"
        elif key == "BACK":
            self.screen = "home"
        elif key == "SEARCH":
            self.screen = "search"
            self.typed_text = ""
        elif key == "PLAY_PAUSE":
            self.media = "paused" if self.media == "playing" else "playing"
        elif key == "MUTE":
            self.muted = not self.muted
        elif key == "VOLUME_UP":
            self.volume = min(100, self.volume + 1)
        elif key == "VOLUME_DOWN":
            self.volume = max(0, self.volume - 1)

    def snapshot(self) -> dict[str, Any]:
        return {
            "screen": self.screen,
            "active_app": self.active_app,
            "query": self.query,
            "media": self.media,
            "muted": self.muted,
            "volume": self.volume,
            "history_count": len(self.history),
        }


def make_connector_simulator_handler(
    secret: bytes,
    state: VirtualTvState,
    profile: str = DEFAULT_CONNECTOR_PROFILE,
):
    profile_keys = CONNECTOR_KEYS_BY_PROFILE.get(profile)
    if profile_keys is None:
        raise TvConnectorError("The connector simulator profile is unsupported.")
    seen_nonces: dict[str, int] = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "RoomAgentConnectorSimulator/1"
        sys_version = ""

        def _json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            authenticate: bool = False,
        ) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if authenticate:
                self.send_header(
                    "X-Room-Agent-Response-Signature",
                    sign_response(secret, body),
                )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/health":
                self._json(404, {"status": "not_found"})
                return
            with lock:
                snapshot = state.snapshot()
            self._json(
                200,
                {
                    "version": CONNECTOR_VERSION,
                    "response_auth": RESPONSE_AUTH_CAPABILITY,
                    "boot_id": state.boot_id,
                    "status": "ok",
                    "device": "virtual-atoms3u",
                    "profile": profile,
                    "transport": "simulated-usb-hid-ir",
                    "capabilities": sorted(profile_keys),
                    "state": snapshot,
                },
            )

        def do_POST(self) -> None:
            if self.path != "/v1/execute":
                self._json(404, {"status": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_CONNECTOR_BODY_BYTES:
                self._json(413, {"status": "rejected", "error": "invalid_body_size"})
                return
            body = self.rfile.read(length)
            try:
                with lock:
                    nonce, expiry, current = verify_request_authentication(
                        secret,
                        body,
                        self.headers,
                    )
                    plan = ConnectorPlan.from_bytes(body)
                    if plan.boot_id != state.boot_id:
                        raise TvConnectorError(
                            "The connector plan belongs to a prior boot."
                        )
                    if plan.profile != profile:
                        raise TvConnectorError(
                            "The connector plan profile does not match the simulator."
                        )
                    consume_verified_nonce(
                        nonce,
                        expiry,
                        seen_nonces,
                        now=current,
                    )
                    snapshot = state.execute(plan)
            except (ProtocolError, TvConnectorError) as exc:
                self._json(403, {"status": "rejected", "error": str(exc)})
                return
            self._json(
                200,
                {
                    "version": CONNECTOR_VERSION,
                    "status": "executed",
                    "request_id": plan.request_id,
                    "profile": plan.profile,
                    "purpose": plan.purpose,
                    "steps_executed": len(plan.steps),
                    "transport": "simulated-usb-hid-ir",
                },
                authenticate=True,
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"CONNECTOR: {self.client_address[0]} - {fmt % args}")

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument(
        "--profile",
        default=DEFAULT_CONNECTOR_PROFILE,
        choices=SUPPORTED_CONNECTOR_PROFILES,
        help="Connector hardware profile to simulate.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1_024 <= args.port <= 65_535:
        print("ERROR: --port must be between 1024 and 65535.")
        return 2
    try:
        secret = read_key(args.key_file)
        server = ThreadingHTTPServer(
            (args.bind, args.port),
            make_connector_simulator_handler(
                secret,
                VirtualTvState(),
                profile=args.profile,
            ),
        )
    except (OSError, ProtocolError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"Virtual ATOMS3U connector ready on http://{args.bind}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping connector simulator.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

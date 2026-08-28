"""Production push-to-talk gateway for an iPhone Action Button Shortcut."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


API_VERSION = 1
MAX_COMMAND_BODY_BYTES = 2_048
MAX_TRANSCRIPT_CHARACTERS = 500
DEFAULT_RATE_LIMIT = 12
DEFAULT_RATE_WINDOW_SECONDS = 60.0
DEFAULT_IDEMPOTENCY_SECONDS = 600.0
DEFAULT_PAIRING_TTL_SECONDS = 600
MAX_PAIRING_BODY_BYTES = 1_024
MAX_PENDING_PAIRINGS = 16
TOKEN_PATTERN = re.compile(r"^ra1\.([0-9a-f]{16})\.([A-Za-z0-9_-]{43})$")
PAIRING_CODE_PATTERN = re.compile(
    r"^rap1\.([0-9a-f]{16})\.([A-Za-z0-9_-]{43})$"
)

_SHORTCUT_ACTIONS = {
    "app.open": ("📺", "App opened"),
    "media.close": ("🏠", "Returned home"),
    "media.pause": ("⏸️", "Playback paused"),
    "media.resume": ("▶️", "Playback resumed"),
    "web.open": ("🌐", "Page opened"),
    "web.open_first_result": ("↗️", "First result opened"),
    "web.search": ("🔎", "Search opened"),
    "youtube.play_latest": ("▶️", "Now playing"),
    "youtube.search_latest": ("▶️", "Now playing"),
    "youtube.search_play": ("▶️", "Now playing"),
}


class IphoneGatewayError(ValueError):
    pass


def _display_fragment(value: Any, *, maximum: int = 72) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    if len(cleaned) <= maximum:
        return cleaned
    return cleaned[: maximum - 1].rstrip() + "…"


def _friendly_opened(value: Any) -> str:
    opened = _display_fragment(value)
    lowered = opened.casefold()
    if "youtube.com" in lowered or lowered == "youtube":
        return "YouTube"
    if "miton.cz" in lowered:
        return "Miton.cz"
    if lowered == "netflix":
        return "Netflix"
    if lowered.startswith("tv-search:"):
        return _display_fragment(opened[len("tv-search:") :])
    if "://" in opened:
        return ""
    return opened


def add_shortcut_presentation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Add a stable, human-friendly card while preserving the command JSON."""

    response = dict(payload)
    status = str(response.get("status", "")).casefold()
    if status not in {"executed", "error", "rejected"}:
        return response

    if status == "executed":
        action = str(response.get("action", ""))
        icon, title = _SHORTCUT_ACTIONS.get(action, ("✅", "Command completed"))
        detail = _display_fragment(response.get("title"))
        if not detail:
            detail = _friendly_opened(response.get("opened"))
        if action == "web.open" and detail:
            title = f"{detail} opened"
            detail = ""
    else:
        icon, title = "⚠️", "Couldn’t complete that"
        detail = _display_fragment(response.get("message"))
        if not detail:
            error = str(response.get("error", "")).replace("_", " ").strip()
            detail = _display_fragment(error.capitalize()) or "Please try again"

    target = response.get("executed_target") or response.get("active_target")
    target_label = {
        "notebook": "Laptop",
        "tv": "Television",
        "tv-connector": "Television",
    }.get(str(target), "Room Agent")
    subtitle_parts = [target_label]
    duration = response.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        duration_label = (
            f"{round(duration)} ms" if duration < 1_000 else f"{duration / 1_000:.1f} s"
        )
        subtitle_parts.append(duration_label)
    subtitle = "  •  ".join(subtitle_parts)

    lines = [f"{icon}  {title}"]
    if detail:
        lines.append(detail)
    lines.append(subtitle)
    response["popup"] = "\n".join(lines)
    response["shortcut_card"] = {
        "style": "room-agent-card-v1",
        "icon": icon,
        "title": title,
        "detail": detail,
        "subtitle": subtitle,
    }
    return response


@dataclass(frozen=True)
class CommandRequest:
    request_id: str
    text: str


@dataclass(frozen=True)
class AuthenticatedDevice:
    device_id: str
    name: str


@dataclass(frozen=True)
class Enrollment:
    device_id: str
    name: str
    token: str
    created_at: str


@dataclass(frozen=True)
class PairingInvitation:
    name: str
    code: str
    created_at: str
    expires_at: str


CommandExecutor = Callable[[str], Mapping[str, Any]]
Authenticator = Callable[[str], AuthenticatedDevice | None]
PairingRedeemer = Callable[[str], Enrollment]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_iphone_state_dir() -> Path:
    if os.name == "nt" and os.getenv("LOCALAPPDATA", "").strip():
        return Path(os.environ["LOCALAPPDATA"]) / "RoomAgent"
    return Path.home() / ".config" / "room-agent"


def default_iphone_registry_path() -> Path:
    return default_iphone_state_dir() / "iphone-devices.json"


def default_iphone_audit_path() -> Path:
    if os.name == "nt":
        return default_iphone_state_dir() / "iphone-audit.jsonl"
    return Path.home() / ".local" / "state" / "room-agent" / "iphone-audit.jsonl"


def _private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def parse_command(body: bytes) -> CommandRequest:
    if not body or len(body) > MAX_COMMAND_BODY_BYTES:
        raise IphoneGatewayError("The command body has an invalid size.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IphoneGatewayError("The command must be UTF-8 JSON.") from exc
    expected = {"version", "request_id", "text"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise IphoneGatewayError(
            "The command must contain exactly version, request_id, and text."
        )
    if payload.get("version") != API_VERSION:
        raise IphoneGatewayError("The command API version is unsupported.")
    raw_request_id = payload.get("request_id")
    if not isinstance(raw_request_id, str):
        raise IphoneGatewayError("The request_id must be text.")
    raw_request_id = " ".join(raw_request_id.split())
    if not raw_request_id or len(raw_request_id) > 200:
        raise IphoneGatewayError("The request_id is empty or too long.")
    try:
        request_id = str(uuid.UUID(raw_request_id))
    except ValueError:
        # Recent Shortcuts releases do not expose Generate UUID on every
        # device. A per-run Current Date value still gives us a stable client
        # nonce; canonicalising it as UUIDv5 keeps the server's idempotency key
        # compact without weakening duplicate suppression.
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"room-agent:{raw_request_id}"))
    transcript = payload.get("text")
    if not isinstance(transcript, str):
        raise IphoneGatewayError("The command text must be a string.")
    transcript = " ".join(transcript.split())
    if not transcript or len(transcript) > MAX_TRANSCRIPT_CHARACTERS:
        raise IphoneGatewayError("The command text is empty or too long.")
    return CommandRequest(request_id=request_id, text=transcript)


def parse_transcript(body: bytes) -> str:
    """Compatibility helper used by tests and small integrations."""

    return parse_command(body).text


def parse_pairing_redemption(body: bytes) -> str:
    """Validate the deliberately tiny unauthenticated pairing exchange."""

    if not body or len(body) > MAX_PAIRING_BODY_BYTES:
        raise IphoneGatewayError("The pairing body has an invalid size.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IphoneGatewayError("The pairing request must be UTF-8 JSON.") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "pairing_code"}
        or payload.get("version") != API_VERSION
    ):
        raise IphoneGatewayError(
            "The pairing request must contain version and pairing_code."
        )
    code = payload.get("pairing_code")
    if not isinstance(code, str) or PAIRING_CODE_PATTERN.fullmatch(code) is None:
        raise IphoneGatewayError("The pairing code is invalid or expired.")
    return code


class IphoneDeviceRegistry:
    """Store only token hashes so the Pi cannot re-display enrolled secrets."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "devices": [], "pairings": []}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IphoneGatewayError(
                f"The iPhone device registry cannot be read: {exc}"
            ) from exc
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(document.get("devices"), list)
        ):
            raise IphoneGatewayError("The iPhone device registry is invalid.")
        for item in document["devices"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("token_sha256"), str)
                or item.get("revoked_at") is not None
                and not isinstance(item.get("revoked_at"), str)
            ):
                raise IphoneGatewayError("The iPhone device registry is invalid.")
        pairings = document.setdefault("pairings", [])
        if not isinstance(pairings, list):
            raise IphoneGatewayError("The iPhone device registry is invalid.")
        for item in pairings:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("code_sha256"), str)
                or not isinstance(item.get("created_at"), str)
                or not isinstance(item.get("expires_at"), str)
            ):
                raise IphoneGatewayError("The iPhone device registry is invalid.")
        return document

    def _write(self, document: Mapping[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _private_file(temporary)
            temporary.replace(self.path)
            _private_file(self.path)
        except OSError as exc:
            raise IphoneGatewayError(
                f"The iPhone device registry cannot be written: {exc}"
            ) from exc

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _new_token(device_id: str) -> str:
        return f"ra1.{device_id}.{secrets.token_urlsafe(32)}"

    @staticmethod
    def _new_pairing_code(pairing_id: str) -> str:
        return f"rap1.{pairing_id}.{secrets.token_urlsafe(32)}"

    @staticmethod
    def _clean_name(name: str) -> str:
        cleaned = " ".join(name.split())
        if not cleaned or len(cleaned) > 80:
            raise IphoneGatewayError("The device name must contain 1 to 80 characters.")
        return cleaned

    def enroll(self, name: str) -> Enrollment:
        name = self._clean_name(name)
        with self._lock:
            document = self._read()
            known = {item.get("id") for item in document["devices"]}
            device_id = secrets.token_hex(8)
            while device_id in known:
                device_id = secrets.token_hex(8)
            token = self._new_token(device_id)
            created_at = _utc_now()
            document["devices"].append(
                {
                    "id": device_id,
                    "name": name,
                    "token_sha256": self._token_digest(token),
                    "created_at": created_at,
                    "revoked_at": None,
                }
            )
            self._write(document)
        return Enrollment(device_id, name, token, created_at)

    def create_pairing(
        self,
        name: str,
        *,
        ttl_seconds: int = DEFAULT_PAIRING_TTL_SECONDS,
    ) -> PairingInvitation:
        """Create a short-lived, one-use code; never store the raw code."""

        name = self._clean_name(name)
        if not 30 <= ttl_seconds <= 3_600:
            raise IphoneGatewayError(
                "The pairing lifetime must be between 30 and 3600 seconds."
            )
        now = datetime.now(timezone.utc)
        created_at = now.isoformat(timespec="seconds")
        expires_at = datetime.fromtimestamp(
            now.timestamp() + ttl_seconds,
            tz=timezone.utc,
        ).isoformat(timespec="seconds")
        with self._lock:
            document = self._read()
            active_pairings = [
                item
                for item in document["pairings"]
                if self._pairing_is_current(item, now)
            ]
            if len(active_pairings) >= MAX_PENDING_PAIRINGS:
                raise IphoneGatewayError("Too many iPhone pairings are pending.")
            known = {item.get("id") for item in active_pairings}
            pairing_id = secrets.token_hex(8)
            while pairing_id in known:
                pairing_id = secrets.token_hex(8)
            code = self._new_pairing_code(pairing_id)
            active_pairings.append(
                {
                    "id": pairing_id,
                    "name": name,
                    "code_sha256": self._token_digest(code),
                    "created_at": created_at,
                    "expires_at": expires_at,
                }
            )
            document["pairings"] = active_pairings
            self._write(document)
        return PairingInvitation(name, code, created_at, expires_at)

    @staticmethod
    def _pairing_is_current(item: Mapping[str, Any], now: datetime) -> bool:
        try:
            expires_at = datetime.fromisoformat(str(item.get("expires_at", "")))
            if expires_at.tzinfo is None:
                return False
            return expires_at > now
        except ValueError:
            return False

    def redeem_pairing(self, code: str) -> Enrollment:
        """Atomically consume a pairing code and issue one device credential."""

        match = PAIRING_CODE_PATTERN.fullmatch(code)
        if match is None:
            raise IphoneGatewayError("The pairing code is invalid or expired.")
        pairing_id = match.group(1)
        supplied_digest = self._token_digest(code)
        now = datetime.now(timezone.utc)
        with self._lock:
            document = self._read()
            matched: dict[str, Any] | None = None
            remaining: list[dict[str, Any]] = []
            for item in document["pairings"]:
                if (
                    item.get("id") == pairing_id
                    and self._pairing_is_current(item, now)
                    and hmac.compare_digest(
                        supplied_digest,
                        str(item.get("code_sha256", "")),
                    )
                ):
                    matched = item
                    continue
                if self._pairing_is_current(item, now):
                    remaining.append(item)
            if matched is None:
                raise IphoneGatewayError("The pairing code is invalid or expired.")

            known = {item.get("id") for item in document["devices"]}
            device_id = secrets.token_hex(8)
            while device_id in known:
                device_id = secrets.token_hex(8)
            token = self._new_token(device_id)
            created_at = _utc_now()
            name = self._clean_name(str(matched.get("name", "iPhone")))
            document["devices"].append(
                {
                    "id": device_id,
                    "name": name,
                    "token_sha256": self._token_digest(token),
                    "created_at": created_at,
                    "revoked_at": None,
                }
            )
            document["pairings"] = remaining
            self._write(document)
        return Enrollment(device_id, name, token, created_at)

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            document = self._read()
            return [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "created_at": item.get("created_at"),
                    "revoked_at": item.get("revoked_at"),
                }
                for item in document["devices"]
            ]

    def revoke(self, device_id: str) -> None:
        with self._lock:
            document = self._read()
            for item in document["devices"]:
                if item.get("id") == device_id:
                    if item.get("revoked_at") is None:
                        item["revoked_at"] = _utc_now()
                        self._write(document)
                    return
        raise IphoneGatewayError(f"No iPhone enrollment has id {device_id!r}.")

    def rotate(self, device_id: str) -> Enrollment:
        with self._lock:
            document = self._read()
            for item in document["devices"]:
                if item.get("id") == device_id:
                    if item.get("revoked_at") is not None:
                        raise IphoneGatewayError("A revoked enrollment cannot be rotated.")
                    token = self._new_token(device_id)
                    item["token_sha256"] = self._token_digest(token)
                    item["rotated_at"] = _utc_now()
                    self._write(document)
                    return Enrollment(
                        device_id,
                        str(item.get("name", "iPhone")),
                        token,
                        str(item.get("created_at", "")),
                    )
        raise IphoneGatewayError(f"No iPhone enrollment has id {device_id!r}.")

    def authenticate(self, token: str) -> AuthenticatedDevice | None:
        match = TOKEN_PATTERN.fullmatch(token)
        if match is None:
            return None
        device_id = match.group(1)
        supplied_digest = self._token_digest(token)
        with self._lock:
            document = self._read()
            for item in document["devices"]:
                if item.get("id") != device_id or item.get("revoked_at") is not None:
                    continue
                expected = str(item.get("token_sha256", ""))
                if hmac.compare_digest(supplied_digest, expected):
                    return AuthenticatedDevice(device_id, str(item.get("name", "iPhone")))
        return None


class SlidingWindowRateLimiter:
    def __init__(
        self,
        limit: int = DEFAULT_RATE_LIMIT,
        window_seconds: float = DEFAULT_RATE_WINDOW_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("The iPhone rate limit must be positive.")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, device_id: str) -> tuple[bool, int]:
        now = self._clock()
        threshold = now - self.window_seconds
        with self._lock:
            events = self._events.setdefault(device_id, deque())
            while events and events[0] <= threshold:
                events.popleft()
            if len(events) >= self.limit:
                retry = max(1, int(self.window_seconds - (now - events[0])) + 1)
                return False, retry
            events.append(now)
        return True, 0


class IdempotencyStore:
    def __init__(
        self,
        ttl_seconds: float = DEFAULT_IDEMPOTENCY_SECONDS,
        max_entries: int = 512,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._items: OrderedDict[
            tuple[str, str], tuple[float, str, int, dict[str, Any]]
        ] = OrderedDict()

    def get(
        self, device_id: str, request: CommandRequest
    ) -> tuple[int, dict[str, Any]] | None:
        now = self._clock()
        key = (device_id, request.request_id)
        expired = [key for key, value in self._items.items() if value[0] <= now]
        for old_key in expired:
            self._items.pop(old_key, None)
        item = self._items.get(key)
        if item is None:
            return None
        _, fingerprint, status, payload = item
        if not hmac.compare_digest(fingerprint, self._fingerprint(request.text)):
            raise IphoneGatewayError("The request_id was reused with different text.")
        self._items.move_to_end(key)
        return status, {**payload, "replayed": True}

    def put(
        self,
        device_id: str,
        request: CommandRequest,
        status: int,
        payload: Mapping[str, Any],
    ) -> None:
        key = (device_id, request.request_id)
        self._items[key] = (
            self._clock() + self.ttl_seconds,
            self._fingerprint(request.text),
            status,
            dict(payload),
        )
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    @staticmethod
    def _fingerprint(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        self.path = path.expanduser() if path is not None else None
        self._lock = threading.Lock()

    def write(self, **event: Any) -> None:
        if self.path is None:
            return
        record = {"occurred_at": _utc_now(), **event}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                _private_file(self.path)
        except OSError as exc:
            # Audit visibility is important, but a full disk must not strand the TV.
            print(f"WARNING: iPhone audit log unavailable: {exc}")


def _static_authenticator(secret: bytes) -> Authenticator:
    if len(secret) != 32:
        raise ValueError("The iPhone gateway token must contain 32 bytes.")
    expected = secret.hex()

    def authenticate(token: str) -> AuthenticatedDevice | None:
        if hmac.compare_digest(token, expected):
            return AuthenticatedDevice("legacy", "Legacy iPhone")
        return None

    return authenticate


def make_iphone_handler(
    authenticator: Authenticator | bytes,
    executor: CommandExecutor,
    *,
    pairing_redeemer: PairingRedeemer | None = None,
    audit_log: AuditLog | None = None,
    rate_limiter: SlidingWindowRateLimiter | None = None,
    pairing_rate_limiter: SlidingWindowRateLimiter | None = None,
    idempotency: IdempotencyStore | None = None,
):
    if isinstance(authenticator, bytes):
        authenticator = _static_authenticator(authenticator)
    audit_log = audit_log or AuditLog(None)
    rate_limiter = rate_limiter or SlidingWindowRateLimiter()
    pairing_rate_limiter = pairing_rate_limiter or SlidingWindowRateLimiter(
        limit=20,
        window_seconds=60,
    )
    idempotency = idempotency or IdempotencyStore()
    execution_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "RoomAgentIphoneGateway/2"
        sys_version = ""

        def _json(
            self,
            status: int,
            payload: Mapping[str, Any],
            *,
            extra_headers: Mapping[str, str] | None = None,
        ) -> None:
            body = json.dumps(
                add_shortcut_presentation(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if extra_headers:
                for name, value in extra_headers.items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/health":
                self._json(404, {"status": "not_found"})
                return
            self._json(
                200,
                {
                    "version": API_VERSION,
                    "status": "ok",
                    "device": "room-agent",
                    "input": "iphone",
                    "capabilities": ["commands", "app-pairing"],
                },
            )

        def do_POST(self) -> None:
            if self.path == "/v1/pairings/redeem":
                self._redeem_pairing()
                return
            if self.path != "/v1/commands":
                self._json(404, {"status": "not_found"})
                return
            supplied = self.headers.get("Authorization", "")
            scheme, separator, token = supplied.partition(" ")
            try:
                device = (
                    authenticator(token)
                    if separator and scheme.casefold() == "bearer" and token
                    else None
                )
            except IphoneGatewayError:
                self._json(
                    503,
                    {"status": "error", "error": "device_registry_unavailable"},
                    extra_headers={"Retry-After": "5"},
                )
                return
            if device is None:
                audit_log.write(
                    event="authentication_rejected",
                    client=self.client_address[0],
                )
                self._json(
                    401,
                    {"status": "rejected", "error": "unauthorized"},
                    extra_headers={"WWW-Authenticate": "Bearer"},
                )
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.strip().casefold() != "application/json":
                self._json(
                    415,
                    {"status": "rejected", "error": "content_type_must_be_json"},
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_COMMAND_BODY_BYTES:
                self._json(
                    413,
                    {"status": "rejected", "error": "invalid_body_size"},
                )
                return
            try:
                request = parse_command(self.rfile.read(length))
            except IphoneGatewayError as exc:
                self._json(400, {"status": "rejected", "error": str(exc)})
                return

            started = time.monotonic()
            with execution_lock:
                try:
                    cached = idempotency.get(device.device_id, request)
                except IphoneGatewayError as exc:
                    self._json(
                        409,
                        {
                            "version": API_VERSION,
                            "request_id": request.request_id,
                            "status": "rejected",
                            "error": str(exc),
                        },
                    )
                    return
                if cached is not None:
                    status, payload = cached
                    audit_log.write(
                        event="command_replay",
                        device_id=device.device_id,
                        device_name=device.name,
                        request_id=request.request_id,
                        status=payload["status"],
                        client=self.client_address[0],
                    )
                    self._json(status, payload)
                    return
                allowed, retry_after = rate_limiter.allow(device.device_id)
                if not allowed:
                    audit_log.write(
                        event="rate_limited",
                        device_id=device.device_id,
                        device_name=device.name,
                        request_id=request.request_id,
                        client=self.client_address[0],
                    )
                    self._json(
                        429,
                        {
                            "version": API_VERSION,
                            "request_id": request.request_id,
                            "status": "rejected",
                            "error": "rate_limited",
                        },
                        extra_headers={"Retry-After": str(retry_after)},
                    )
                    return
                try:
                    result = dict(executor(request.text))
                    status = 200
                    payload = {
                        "version": API_VERSION,
                        "request_id": request.request_id,
                        "status": "executed",
                        **result,
                    }
                except Exception as exc:
                    status = 422
                    payload = {
                        "version": API_VERSION,
                        "request_id": request.request_id,
                        "status": "error",
                        "error": "command_failed",
                        "message": str(exc)[:240],
                    }
                payload["duration_ms"] = round((time.monotonic() - started) * 1000)
                idempotency.put(device.device_id, request, status, payload)

            audit_log.write(
                event="command",
                device_id=device.device_id,
                device_name=device.name,
                request_id=request.request_id,
                status=payload["status"],
                duration_ms=payload["duration_ms"],
                client=self.client_address[0],
            )
            self._json(status, payload)

        def _redeem_pairing(self) -> None:
            if pairing_redeemer is None:
                self._json(404, {"status": "not_found"})
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.strip().casefold() != "application/json":
                self._json(
                    415,
                    {"status": "rejected", "error": "content_type_must_be_json"},
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_PAIRING_BODY_BYTES:
                self._json(
                    413,
                    {"status": "rejected", "error": "invalid_body_size"},
                )
                return
            allowed, retry_after = pairing_rate_limiter.allow(self.client_address[0])
            if not allowed:
                self._json(
                    429,
                    {"status": "rejected", "error": "rate_limited"},
                    extra_headers={"Retry-After": str(retry_after)},
                )
                return
            try:
                code = parse_pairing_redemption(self.rfile.read(length))
                enrollment = pairing_redeemer(code)
            except IphoneGatewayError:
                audit_log.write(
                    event="pairing_rejected",
                    client=self.client_address[0],
                )
                self._json(
                    400,
                    {"status": "rejected", "error": "invalid_or_expired_pairing"},
                )
                return
            audit_log.write(
                event="pairing_completed",
                device_id=enrollment.device_id,
                device_name=enrollment.name,
                client=self.client_address[0],
            )
            self._json(
                201,
                {
                    "version": API_VERSION,
                    "status": "paired",
                    "device_id": enrollment.device_id,
                    "device_name": enrollment.name,
                    "access_token": enrollment.token,
                    "commands_path": "/v1/commands",
                },
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"IPHONE: {self.client_address[0]} - {fmt % args}")

    return Handler


class SingleInstanceThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent two Windows gateway processes from sharing one loopback port."""

    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()


class IphoneCommandServer:
    def __init__(
        self,
        authenticator: Authenticator | bytes,
        executor: CommandExecutor,
        *,
        pairing_redeemer: PairingRedeemer | None = None,
        host: str = "127.0.0.1",
        port: int = 8770,
        audit_path: Path | None = None,
        server_factory: Callable[..., ThreadingHTTPServer] | None = None,
    ) -> None:
        if not 1_024 <= port <= 65_535:
            raise ValueError("The iPhone gateway port must be between 1024 and 65535.")
        handler = make_iphone_handler(
            authenticator,
            executor,
            pairing_redeemer=pairing_redeemer,
            audit_log=AuditLog(audit_path),
        )
        factory = server_factory or SingleInstanceThreadingHTTPServer
        self._server = factory((host, port), handler)

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()

    def close(self) -> None:
        self._server.server_close()

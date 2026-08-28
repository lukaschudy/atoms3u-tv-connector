"""Bounded, authenticated client protocol for the ATOMS3U USB TV connector."""

from __future__ import annotations

import asyncio
import http.client
import json
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from protocol import ProtocolError, signed_headers, verify_response


CONNECTOR_VERSION = 2
RESPONSE_AUTH_CAPABILITY = "hmac-sha256-v1"
MAX_CONNECTOR_BODY_BYTES = 8_192
MAX_CONNECTOR_STEPS = 32
MAX_CONNECTOR_TEXT_CHARACTERS = 120
MAX_CONNECTOR_TOTAL_WAIT_MS = 20_000
MAX_CONNECTOR_WAIT_MS = 5_000
DEFAULT_CONNECTOR_PROFILE = "generic-usb-hid-v1"
SONY_BRAVIA_IR_PROFILE = "sony-bravia-ir-v1"
LG_TV_IR_PROFILE = "lg-tv-ir-v1"
LG_WEBOS_HYBRID_PROFILE = "lg-webos-hybrid-v1"
DEFAULT_CONNECTOR_BLE_NAME = "atoms3u-tv-01"
BLE_SERVICE_UUID = "82a15f10-2b7f-4dc4-a9a5-8c841f4a1000"
BLE_COMMAND_UUID = "82a15f10-2b7f-4dc4-a9a5-8c841f4a1001"
BLE_RESPONSE_UUID = "82a15f10-2b7f-4dc4-a9a5-8c841f4a1002"
BLE_FRAME_START = 0x01
BLE_FRAME_DATA = 0x02
BLE_FRAME_END = 0x03
BLE_MIN_DATA_BYTES = 16
BLE_MAX_DATA_BYTES = 160
GENERIC_USB_CONNECTOR_KEYS = frozenset(
    {
        "POWER",
        "HOME",
        "BACK",
        "UP",
        "DOWN",
        "LEFT",
        "RIGHT",
        "ENTER",
        "SEARCH",
        "PLAY_PAUSE",
        "MUTE",
        "VOLUME_UP",
        "VOLUME_DOWN",
    }
)
SONY_BRAVIA_IR_KEYS = frozenset(
    {
        "POWER",
        "POWER_ON",
        "POWER_OFF",
        "HOME",
        "BACK",
        "UP",
        "DOWN",
        "LEFT",
        "RIGHT",
        "ENTER",
        "PLAY",
        "PAUSE",
        "MUTE",
        "VOLUME_UP",
        "VOLUME_DOWN",
        "INPUT",
        "NETFLIX",
    }
)
LG_TV_IR_KEYS = frozenset(
    {
        "POWER",
        "POWER_ON",
        "POWER_OFF",
        "HOME",
        "BACK",
        "UP",
        "DOWN",
        "LEFT",
        "RIGHT",
        "ENTER",
        "PLAY",
        "PAUSE",
        "MUTE",
        "VOLUME_UP",
        "VOLUME_DOWN",
        "INPUT",
    }
)
LG_WEBOS_HYBRID_KEYS = LG_TV_IR_KEYS | frozenset({"SEARCH"})
CONNECTOR_KEYS_BY_PROFILE = {
    DEFAULT_CONNECTOR_PROFILE: GENERIC_USB_CONNECTOR_KEYS,
    SONY_BRAVIA_IR_PROFILE: SONY_BRAVIA_IR_KEYS,
    LG_TV_IR_PROFILE: LG_TV_IR_KEYS,
    LG_WEBOS_HYBRID_PROFILE: LG_WEBOS_HYBRID_KEYS,
}
SUPPORTED_CONNECTOR_PROFILES = tuple(CONNECTOR_KEYS_BY_PROFILE)
ALLOWED_CONNECTOR_KEYS = frozenset().union(*CONNECTOR_KEYS_BY_PROFILE.values())
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_BOOT_ID = re.compile(r"^[0-9a-f]{32}$")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SAFE_TEXT = re.compile(r"^[\x20-\x7e]+$")


class TvConnectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectorStep:
    op: str
    value: str | int

    @classmethod
    def key(cls, name: str) -> "ConnectorStep":
        return cls("key", name)

    @classmethod
    def text(cls, value: str) -> "ConnectorStep":
        return cls("text", value)

    @classmethod
    def wait(cls, milliseconds: int) -> "ConnectorStep":
        return cls("wait", milliseconds)

    def to_dict(self) -> dict[str, str | int]:
        field = "ms" if self.op == "wait" else self.op
        return {"op": self.op, field: self.value}


@dataclass(frozen=True)
class ConnectorPlan:
    request_id: str
    boot_id: str
    profile: str
    purpose: str
    steps: tuple[ConnectorStep, ...]
    version: int = CONNECTOR_VERSION

    @classmethod
    def create(
        cls,
        purpose: str,
        steps: Iterable[ConnectorStep],
        *,
        boot_id: str,
        profile: str = DEFAULT_CONNECTOR_PROFILE,
    ) -> "ConnectorPlan":
        plan = cls(
            request_id=str(uuid.uuid4()),
            boot_id=boot_id,
            profile=profile,
            purpose=purpose,
            steps=tuple(steps),
        )
        plan.validate()
        return plan

    def validate(self) -> None:
        if not isinstance(self.request_id, str):
            raise TvConnectorError("The connector request_id must be a UUID.")
        try:
            parsed_request_id = uuid.UUID(self.request_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise TvConnectorError("The connector request_id must be a UUID.") from exc
        if str(parsed_request_id) != self.request_id:
            raise TvConnectorError(
                "The connector request_id must be a canonical UUID."
            )
        if type(self.version) is not int or self.version != CONNECTOR_VERSION:
            raise TvConnectorError("The connector protocol version is unsupported.")
        if not isinstance(self.boot_id, str) or _BOOT_ID.fullmatch(self.boot_id) is None:
            raise TvConnectorError(
                "The connector boot_id must be 32 lowercase hex characters."
            )
        if not isinstance(self.profile, str) or not _NAME.fullmatch(self.profile):
            raise TvConnectorError("The connector profile name is invalid.")
        profile_keys = CONNECTOR_KEYS_BY_PROFILE.get(self.profile)
        if profile_keys is None:
            raise TvConnectorError("The connector profile is unsupported.")
        if not isinstance(self.purpose, str) or not _NAME.fullmatch(self.purpose):
            raise TvConnectorError("The connector purpose is invalid.")
        if not 1 <= len(self.steps) <= MAX_CONNECTOR_STEPS:
            raise TvConnectorError("A connector plan must contain 1 to 32 steps.")

        total_wait = 0
        for step in self.steps:
            if step.op == "key":
                if not isinstance(step.value, str) or step.value not in profile_keys:
                    raise TvConnectorError("The connector plan contains an unsupported key.")
            elif step.op == "text":
                if self.profile not in {
                    DEFAULT_CONNECTOR_PROFILE,
                    LG_WEBOS_HYBRID_PROFILE,
                }:
                    raise TvConnectorError(
                        "Connector text is unavailable for infrared profiles."
                    )
                if (
                    not isinstance(step.value, str)
                    or not 1 <= len(step.value) <= MAX_CONNECTOR_TEXT_CHARACTERS
                    or _SAFE_TEXT.fullmatch(step.value) is None
                ):
                    raise TvConnectorError(
                        "Connector text must contain 1 to 120 printable ASCII characters."
                    )
            elif step.op == "wait":
                if (
                    isinstance(step.value, bool)
                    or not isinstance(step.value, int)
                    or not 0 <= step.value <= MAX_CONNECTOR_WAIT_MS
                ):
                    raise TvConnectorError("Connector waits must be between 0 and 5000 ms.")
                total_wait += step.value
            else:
                raise TvConnectorError("The connector plan contains an unsupported operation.")
        if total_wait > MAX_CONNECTOR_TOTAL_WAIT_MS:
            raise TvConnectorError("The connector plan waits for too long.")

    def to_bytes(self) -> bytes:
        self.validate()
        payload = {
            "version": self.version,
            "request_id": self.request_id,
            "boot_id": self.boot_id,
            "profile": self.profile,
            "purpose": self.purpose,
            "steps": [step.to_dict() for step in self.steps],
        }
        body = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if len(body) > MAX_CONNECTOR_BODY_BYTES:
            raise TvConnectorError("The connector request is too large.")
        return body

    @classmethod
    def from_bytes(cls, body: bytes) -> "ConnectorPlan":
        if not body or len(body) > MAX_CONNECTOR_BODY_BYTES:
            raise TvConnectorError("The connector request has an invalid size.")
        try:
            payload = json.loads(body.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TvConnectorError("The connector request must be ASCII JSON.") from exc
        expected = {
            "version",
            "request_id",
            "boot_id",
            "profile",
            "purpose",
            "steps",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise TvConnectorError("The connector request fields do not match the schema.")
        if not isinstance(payload["steps"], list):
            raise TvConnectorError("The connector steps must be an array.")

        steps: list[ConnectorStep] = []
        for raw_step in payload["steps"]:
            if not isinstance(raw_step, dict) or "op" not in raw_step:
                raise TvConnectorError("Each connector step must be an object.")
            op = raw_step["op"]
            if not isinstance(op, str):
                raise TvConnectorError("The connector step operation must be text.")
            field = "ms" if op == "wait" else op
            if set(raw_step) != {"op", field}:
                raise TvConnectorError("A connector step has unexpected fields.")
            steps.append(ConnectorStep(op, raw_step[field]))

        plan = cls(
            version=payload["version"],
            request_id=payload["request_id"],
            boot_id=payload["boot_id"],
            profile=payload["profile"],
            purpose=payload["purpose"],
            steps=tuple(steps),
        )
        plan.validate()
        return plan


def _clean_text(value: str, *, maximum: int = MAX_CONNECTOR_TEXT_CHARACTERS) -> str:
    value = " ".join(value.split())
    if not value or len(value) > maximum or _SAFE_TEXT.fullmatch(value) is None:
        raise TvConnectorError("The TV connector text is invalid or unsupported.")
    return value


def _open_app_steps(
    app: str,
    *,
    search_ready_ms: int = 400,
    results_ready_ms: int = 250,
    launch_ready_ms: int = 1_500,
) -> tuple[ConnectorStep, ...]:
    app = _clean_text(app, maximum=40)
    return (
        ConnectorStep.key("HOME"),
        ConnectorStep.wait(1_200),
        ConnectorStep.key("SEARCH"),
        ConnectorStep.wait(search_ready_ms),
        ConnectorStep.text(app),
        ConnectorStep.wait(results_ready_ms),
        ConnectorStep.key("ENTER"),
        ConnectorStep.wait(launch_ready_ms),
    )


class TvConnectorClient:
    """Translate room actions into bounded USB-HID/IR connector plans."""

    def __init__(
        self,
        secret: bytes,
        *,
        profile: str = DEFAULT_CONNECTOR_PROFILE,
        timeout: float = 8.0,
    ) -> None:
        if len(secret) != 32:
            raise TvConnectorError("The TV connector key must contain 32 bytes.")
        if not _NAME.fullmatch(profile):
            raise TvConnectorError("The TV connector profile name is invalid.")
        if profile not in CONNECTOR_KEYS_BY_PROFILE:
            raise TvConnectorError("The TV connector profile is unsupported.")
        if not 0.5 <= timeout <= 30:
            raise TvConnectorError("The TV connector timeout must be between 0.5 and 30 seconds.")
        self.secret = secret
        self.profile = profile
        self.timeout = timeout
        self._media_state: str | None = None
        self._last_search: str | None = None

    @property
    def is_sony_ir(self) -> bool:
        return self.profile == SONY_BRAVIA_IR_PROFILE

    @property
    def is_lg_ir(self) -> bool:
        return self.profile == LG_TV_IR_PROFILE

    @property
    def is_lg_hybrid(self) -> bool:
        return self.profile == LG_WEBOS_HYBRID_PROFILE

    @property
    def is_ir(self) -> bool:
        return self.is_sony_ir or self.is_lg_ir or self.is_lg_hybrid

    @property
    def supports_usb_text(self) -> bool:
        return self.profile in {DEFAULT_CONNECTOR_PROFILE, LG_WEBOS_HYBRID_PROFILE}

    def health(self) -> dict[str, Any]:
        raise NotImplementedError

    def _execute(self, purpose: str, steps: Iterable[ConnectorStep]) -> dict[str, str]:
        raise NotImplementedError

    def verify(self) -> dict[str, str]:
        """Authenticate the connector without emitting a TV command."""

        return self._execute("connector.verify", (ConnectorStep.wait(0),))

    def launch_app(self, app: str) -> dict[str, str]:
        app = _clean_text(app, maximum=40)
        self._last_search = None
        if self.is_ir and not self.is_lg_hybrid:
            if not self.is_sony_ir or app.casefold() != "netflix":
                raise TvConnectorError(
                    "The selected infrared profile has no verified key for that app."
                )
            result = self._execute("app.open", (ConnectorStep.key("NETFLIX"),))
            result.update({"opened": "Netflix"})
            return result
        steps = (
            _open_app_steps(
                app,
                search_ready_ms=2_000,
                results_ready_ms=1_200,
                launch_ready_ms=2_500,
            )
            if self.is_lg_hybrid
            else _open_app_steps(app)
        )
        result = self._execute("app.open", steps)
        result.update({"opened": app})
        return result

    def open_site(self, site: str) -> dict[str, str]:
        if site == "youtube":
            return self.launch_app("YouTube")
        if site != "miton":
            raise TvConnectorError("The requested TV site is not allowlisted.")
        return self.search_google("miton.cz")

    def search_google(self, query: str) -> dict[str, str]:
        query = _clean_text(query)
        if not self.supports_usb_text:
            raise TvConnectorError(
                "Text search is unavailable through an infrared remote profile."
            )
        self._last_search = query
        steps = (
            ConnectorStep.key("HOME"),
            ConnectorStep.wait(1_200),
            ConnectorStep.key("SEARCH"),
            ConnectorStep.wait(400),
            ConnectorStep.text(query),
            ConnectorStep.key("ENTER"),
            ConnectorStep.wait(1_200),
        )
        result = self._execute("web.search", steps)
        result.update({"opened": f"tv-search:{query}"})
        return result

    def open_first_google_result(self) -> dict[str, str]:
        if self._last_search is None:
            raise TvConnectorError("There is no active TV search to continue.")
        return self._execute("web.open_first", (ConnectorStep.key("ENTER"),))

    def play_youtube(self, video_id: str) -> dict[str, str]:
        if _VIDEO_ID.fullmatch(video_id) is None:
            raise TvConnectorError("The YouTube video id is invalid.")
        if not self.supports_usb_text:
            raise TvConnectorError(
                "Direct YouTube video selection needs the TV online or an HDMI player."
            )
        if self.is_lg_hybrid:
            steps = (
                *_open_app_steps(
                    "YouTube",
                    search_ready_ms=2_000,
                    results_ready_ms=1_200,
                    launch_ready_ms=2_500,
                ),
                ConnectorStep.key("SEARCH"),
                ConnectorStep.wait(1_500),
                ConnectorStep.text(video_id),
                ConnectorStep.wait(1_200),
                ConnectorStep.key("ENTER"),
                ConnectorStep.wait(2_000),
            )
        else:
            steps = (
                *_open_app_steps("YouTube"),
                ConnectorStep.key("SEARCH"),
                ConnectorStep.text(video_id),
                ConnectorStep.key("ENTER"),
                ConnectorStep.wait(1_500),
            )
        self._media_state = "playing"
        self._last_search = None
        result = self._execute("youtube.play", steps)
        result.update({"opened": f"youtube:{video_id}"})
        return result

    def pause(self) -> dict[str, str]:
        if self._media_state is None and not self.is_ir:
            raise TvConnectorError("There is no active connector playback session.")
        if self._media_state != "paused":
            key = "PAUSE" if self.is_ir else "PLAY_PAUSE"
            self._execute("media.pause", (ConnectorStep.key(key),))
            self._media_state = "paused"
        return {"media_status": "Paused", "player": "atoms3u-tv-connector"}

    def resume(self) -> dict[str, str]:
        if self._media_state is None and not self.is_ir:
            raise TvConnectorError("There is no active connector playback session.")
        if self._media_state != "playing":
            key = "PLAY" if self.is_ir else "PLAY_PAUSE"
            self._execute("media.resume", (ConnectorStep.key(key),))
            self._media_state = "playing"
        return {"media_status": "Playing", "player": "atoms3u-tv-connector"}

    def close(self) -> dict[str, str]:
        self._execute("media.close", (ConnectorStep.key("HOME"),))
        self._media_state = None
        self._last_search = None
        return {"media_status": "Closed", "player": "atoms3u-tv-connector"}

    def press_key(self, key: str, *, count: int = 1) -> dict[str, str]:
        """Send one allowlisted remote key a bounded number of times."""

        key = key.strip().upper()
        if key not in CONNECTOR_KEYS_BY_PROFILE[self.profile]:
            raise TvConnectorError("The requested remote key is not allowlisted.")
        if isinstance(count, bool) or not 1 <= count <= 20:
            raise TvConnectorError("Remote key count must be between 1 and 20.")
        # Firmware supplies each transport's physical timing: USB tap duration,
        # Sony's required burst, or LG's inter-frame gap. Extra wait steps here
        # would make a 20-key request exceed the 32-step protocol limit.
        steps = [ConnectorStep.key(key) for _ in range(count)]
        return self._execute(f"remote.{key.casefold()}", steps)

    def power_toggle(self) -> dict[str, str]:
        return self.press_key("POWER")

    def power_on(self) -> dict[str, str]:
        if not self.is_ir:
            raise TvConnectorError("Discrete power-on requires an infrared TV profile.")
        return self.press_key("POWER_ON")

    def power_off(self) -> dict[str, str]:
        if not self.is_ir:
            raise TvConnectorError("Discrete power-off requires an infrared TV profile.")
        return self.press_key("POWER_OFF")

    def mute(self) -> dict[str, str]:
        return self.press_key("MUTE")

    def volume_up(self, count: int = 1) -> dict[str, str]:
        return self.press_key("VOLUME_UP", count=count)

    def volume_down(self, count: int = 1) -> dict[str, str]:
        return self.press_key("VOLUME_DOWN", count=count)

    def navigate(self, direction: str, count: int = 1) -> dict[str, str]:
        key = direction.strip().upper()
        if key not in {"UP", "DOWN", "LEFT", "RIGHT"}:
            raise TvConnectorError("Navigation direction must be up, down, left, or right.")
        return self.press_key(key, count=count)

    def select(self) -> dict[str, str]:
        return self.press_key("ENTER")

    def home(self) -> dict[str, str]:
        return self.press_key("HOME")

    def back(self) -> dict[str, str]:
        return self.press_key("BACK")

    def input_source(self) -> dict[str, str]:
        if not self.is_ir:
            raise TvConnectorError("The selected profile has no verified input key.")
        return self.press_key("INPUT")


class UsbTvConnectorClient(TvConnectorClient):
    """Deliver authenticated connector plans over the local HTTP fallback."""

    def __init__(
        self,
        base_url: str,
        secret: bytes,
        *,
        profile: str = DEFAULT_CONNECTOR_PROFILE,
        timeout: float = 8.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        candidate = base_url
        if (
            any(ord(character) <= 0x20 or ord(character) > 0x7E for character in candidate)
            or "\\" in candidate
        ):
            raise TvConnectorError("The TV connector URL must be an HTTP(S) origin.")
        try:
            parsed = urllib.parse.urlsplit(candidate)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise TvConnectorError(
                "The TV connector URL must be an HTTP(S) origin."
            ) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not hostname
            or parsed.path not in {"", "/"}
            or port == 0
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise TvConnectorError("The TV connector URL must be an HTTP(S) origin.")
        super().__init__(secret, profile=profile, timeout=timeout)
        self.base_url = candidate[:-1] if candidate.endswith("/") else candidate
        self.opener = opener

    def health(self) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/health",
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                response_body = response.read(MAX_CONNECTOR_BODY_BYTES + 1)
                if len(response_body) > MAX_CONNECTOR_BODY_BYTES:
                    raise TvConnectorError("The TV connector health response is too large.")
                payload = json.loads(response_body.decode("utf-8"))
        except (
            OSError,
            ValueError,
            http.client.HTTPException,
            UnicodeDecodeError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as exc:
            raise TvConnectorError(f"The TV connector health check failed: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise TvConnectorError("The TV connector returned an invalid health response.")
        if payload.get("profile") != self.profile:
            raise TvConnectorError("The TV connector profile does not match the host.")
        return payload

    def _require_execution_compatibility(self) -> str:
        health = self.health()
        boot_id = health.get("boot_id")
        if (
            type(health.get("version")) is not int
            or health.get("version") != CONNECTOR_VERSION
            or health.get("response_auth") != RESPONSE_AUTH_CAPABILITY
            or not isinstance(boot_id, str)
            or _BOOT_ID.fullmatch(boot_id) is None
        ):
            raise TvConnectorError(
                "The TV connector firmware is incompatible with authenticated "
                "responses; reflash it before sending commands."
            )
        return boot_id

    def _execute(self, purpose: str, steps: Iterable[ConnectorStep]) -> dict[str, str]:
        boot_id = self._require_execution_compatibility()
        if purpose != "connector.verify":
            self._post_authenticated_plan(
                "connector.verify",
                (ConnectorStep.wait(0),),
                boot_id,
            )
        return self._post_authenticated_plan(purpose, steps, boot_id)

    def _post_authenticated_plan(
        self,
        purpose: str,
        steps: Iterable[ConnectorStep],
        boot_id: str,
    ) -> dict[str, str]:
        plan = ConnectorPlan.create(
            purpose,
            steps,
            boot_id=boot_id,
            profile=self.profile,
        )
        body = plan.to_bytes()
        request = urllib.request.Request(
            f"{self.base_url}/v1/execute",
            data=body,
            headers=signed_headers(self.secret, body),
            method="POST",
        )
        wait_seconds = sum(
            step.value for step in plan.steps if step.op == "wait"
        ) / 1000
        try:
            with self.opener(
                request,
                timeout=self.timeout + wait_seconds,
            ) as response:
                response_body = response.read(MAX_CONNECTOR_BODY_BYTES + 1)
                if len(response_body) > MAX_CONNECTOR_BODY_BYTES:
                    raise TvConnectorError("The TV connector execution response is too large.")
                signature = response.headers.get(
                    "X-Room-Agent-Response-Signature",
                    "",
                )
                verify_response(self.secret, response_body, signature)
                payload = json.loads(response_body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(MAX_CONNECTOR_BODY_BYTES).decode("utf-8", errors="replace")
            raise TvConnectorError(
                f"The TV connector rejected {purpose}: HTTP {exc.code} {detail[:240]}"
            ) from exc
        except ProtocolError as exc:
            raise TvConnectorError(str(exc)) from exc
        except (
            OSError,
            ValueError,
            http.client.HTTPException,
            UnicodeDecodeError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as exc:
            raise TvConnectorError(f"The TV connector request failed: {exc}") from exc
        required = {
            "version",
            "status",
            "request_id",
            "profile",
            "purpose",
            "steps_executed",
            "transport",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or type(payload.get("version")) is not int
            or payload.get("version") != CONNECTOR_VERSION
            or payload.get("status") != "executed"
            or payload.get("request_id") != plan.request_id
            or payload.get("profile") != plan.profile
            or payload.get("purpose") != plan.purpose
            or type(payload.get("steps_executed")) is not int
            or payload.get("steps_executed") != len(plan.steps)
            or not isinstance(payload.get("transport"), str)
            or not 1 <= len(payload["transport"]) <= 64
        ):
            raise TvConnectorError("The TV connector returned an invalid execution response.")
        return _execution_result(plan, "wifi")


def build_ble_frames(
    secret: bytes,
    body: bytes,
    *,
    chunk_size: int = BLE_MAX_DATA_BYTES,
    now: int | None = None,
) -> tuple[bytes, ...]:
    """Frame one signed connector request for a bounded BLE GATT transfer."""

    if len(secret) != 32:
        raise TvConnectorError("The TV connector key must contain 32 bytes.")
    if not body or len(body) > MAX_CONNECTOR_BODY_BYTES:
        raise TvConnectorError("The connector request has an invalid size.")
    if not BLE_MIN_DATA_BYTES <= chunk_size <= BLE_MAX_DATA_BYTES:
        raise TvConnectorError("The Bluetooth connector chunk size is invalid.")
    headers = signed_headers(secret, body, now=now)
    timestamp = int(headers["X-Room-Agent-Timestamp"])
    nonce = bytes.fromhex(headers["X-Room-Agent-Nonce"])
    signature = bytes.fromhex(headers["X-Room-Agent-Signature"])
    start = struct.pack("<BBHI", BLE_FRAME_START, CONNECTOR_VERSION, len(body), timestamp)
    frames = [start + nonce + signature]
    for offset in range(0, len(body), chunk_size):
        frames.append(
            bytes((BLE_FRAME_DATA,))
            + struct.pack("<H", offset)
            + body[offset : offset + chunk_size]
        )
    frames.append(bytes((BLE_FRAME_END,)))
    return tuple(frames)


def _execution_result(plan: ConnectorPlan, transport: str) -> dict[str, str]:
    return {
        "player": "atoms3u-tv-connector",
        "connector_profile": plan.profile,
        "connector_purpose": plan.purpose,
        "connector_request_id": plan.request_id,
        "connector_transport": transport,
    }


class _BleRuntime:
    """Keep one BLE connection alive on a dedicated asyncio loop."""

    def __init__(self, device_name: str, scan_timeout: float) -> None:
        self.device_name = device_name
        self.scan_timeout = scan_timeout
        self.connect_timeout = 25.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="room-agent-ble",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(3):
            raise TvConnectorError("The Bluetooth connector worker did not start.")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    async def _ensure_client(self) -> Any:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise TvConnectorError(
                "Bluetooth support is not installed; install the 'bleak' package."
            ) from exc
        if self._client is not None and self._client.is_connected:
            return self._client
        device = await BleakScanner.find_device_by_filter(
            lambda candidate, advertisement: (
                candidate.name == self.device_name
                or self.device_name == advertisement.local_name
                or BLE_SERVICE_UUID.lower()
                in {value.lower() for value in advertisement.service_uuids}
            ),
            timeout=self.scan_timeout,
        )
        if device is None:
            raise TvConnectorError(
                f"Bluetooth TV connector {self.device_name!r} was not found."
            )
        # Pairing is deliberately unnecessary. Every execution request is
        # independently HMAC-authenticated and replay protected, which avoids
        # fragile OS-specific bonding state on headless connectors.
        self._client = BleakClient(
            device,
            timeout=self.connect_timeout,
            pair=False,
            services=[BLE_SERVICE_UUID],
        )
        await self._client.connect()
        return self._client

    async def _health(self) -> dict[str, Any]:
        client = await self._ensure_client()
        health: dict[str, Any] = {
            "version": CONNECTOR_VERSION,
            "status": "ok",
            "device": self.device_name,
            "transport": "ble-to-usb-hid",
            "connected": bool(client.is_connected),
        }
        try:
            raw = await client.read_gatt_char(BLE_RESPONSE_UUID)
            connector_status = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            connector_status = None
        if isinstance(connector_status, dict):
            for key in ("usb_initialized", "usb_mounted"):
                if key in connector_status:
                    health[key] = bool(connector_status[key])
        return health

    async def _exchange(
        self,
        frames: tuple[bytes, ...],
        request_id: str,
        response_timeout: float,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        for frame in frames:
            await client.write_gatt_char(BLE_COMMAND_UUID, frame, response=True)
        deadline = time.monotonic() + response_timeout
        while time.monotonic() < deadline:
            raw = await client.read_gatt_char(BLE_RESPONSE_UUID)
            try:
                payload = json.loads(bytes(raw).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                if payload.get("status") == "rejected":
                    raise TvConnectorError(
                        f"The Bluetooth TV connector rejected the command: "
                        f"{payload.get('error', 'unknown_error')}"
                    )
                if (
                    payload.get("status") == "executed"
                    and payload.get("request_id") == request_id
                ):
                    return payload
            await asyncio.sleep(0.1)
        raise TvConnectorError("The Bluetooth TV connector did not finish in time.")

    def health(self, timeout: float) -> dict[str, Any]:
        return self._submit(
            self._health(),
            timeout + self.scan_timeout + self.connect_timeout + 2,
        )

    def exchange(
        self,
        frames: tuple[bytes, ...],
        request_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        return self._submit(
            self._exchange(frames, request_id, timeout),
            timeout + self.scan_timeout + self.connect_timeout + 3,
        )

    def _submit(self, coroutine: Any, timeout: float) -> Any:
        if self._loop is None:
            raise TvConnectorError("The Bluetooth connector worker is unavailable.")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=timeout)
        except TvConnectorError:
            raise
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise TvConnectorError(
                f"Bluetooth TV connector communication failed: {detail}"
            ) from exc


class BleTvConnectorClient(TvConnectorClient):
    """Compatibility sentinel for the disabled legacy Bluetooth transport."""

    def __init__(
        self,
        device_name: str,
        secret: bytes,
        *,
        profile: str = DEFAULT_CONNECTOR_PROFILE,
        timeout: float = 8.0,
        scan_timeout: float = 8.0,
        runtime: Any | None = None,
    ) -> None:
        raise TvConnectorError(
            "Bluetooth connector transport is disabled until protocol v2 response "
            "authentication is implemented; use the production Wi-Fi transport."
        )

    def health(self) -> dict[str, Any]:
        return self._runtime.health(self.timeout)

    def _execute(self, purpose: str, steps: Iterable[ConnectorStep]) -> dict[str, str]:
        plan = ConnectorPlan.create(
            purpose,
            steps,
            boot_id="0" * 32,
            profile=self.profile,
        )
        body = plan.to_bytes()
        frames = build_ble_frames(self.secret, body, chunk_size=BLE_MIN_DATA_BYTES)
        wait_seconds = sum(
            step.value for step in plan.steps if step.op == "wait"
        ) / 1000
        self._runtime.exchange(frames, plan.request_id, self.timeout + wait_seconds)
        return _execution_result(plan, "bluetooth")

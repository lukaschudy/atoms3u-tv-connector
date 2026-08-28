"""Restricted Hermes Agent sidecar adapter for semantic room planning."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from local_llm import (
    MAX_RESPONSE_BYTES,
    LlmError,
    LlmRouter,
)


DEFAULT_HERMES_MODEL = "hermes-agent"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_HERMES_SYSTEM_PROMPT = (
    "You are the bounded planning sidecar for a Raspberry Pi room assistant. "
    "Interpret one English or Czech voice command. You may use only the tools "
    "the operator exposed to you, and only when current web information or "
    "podcast identity disambiguation is genuinely useful. Never use a browser "
    "to start playback or control a device; the trusted room executor does that. "
    "Finish with exactly one compact JSON object and no markdown or explanation. "
    "The object must have exactly action, target, query. action is play_latest, "
    "open, search, pause, resume, close, remote, or unsupported. target is tv or notebook. "
    "Big screen and television mean tv; laptop and computer mean notebook. query "
    "contains only the show, channel, website, native TV app, person, content title, or useful "
    "search question. Remove request verbs, device words, latest, newest, episode, "
    "podcast, and please. Preserve proper names. For pause, resume, or close, query "
    "is empty. For remote, target is tv and query is one canonical command: "
    "power_on, power_off, mute, home, back, select, input, navigate_up, "
    "navigate_down, navigate_left, navigate_right, volume_up, or volume_down; "
    "append a number from 1 to 20 for repetitions. Use search for explicit web "
    "searches and factual questions. Use "
    "unsupported when no listed room action applies."
)


class HermesLlmError(LlmError):
    pass


HermesRequester = Callable[
    [str, dict[str, Any], float, str],
    dict[str, Any],
]


def _validate_hermes_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Hermes must use an authenticated loopback HTTP URL.")
    return base_url.rstrip("/")


def request_hermes_json(
    endpoint: str,
    payload: dict[str, Any],
    timeout: float,
    api_key: str,
) -> dict[str, Any]:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        messages = {
            400: "Hermes rejected the planning request.",
            401: "Hermes sidecar authentication failed.",
            403: "Hermes refused the planning request.",
            408: "Hermes planning timed out.",
            429: "Hermes is already handling another request.",
            502: "Hermes could not reach its model provider.",
            503: "Hermes is not ready.",
        }
        raise HermesLlmError(
            messages.get(exc.code, f"Hermes request failed with HTTP {exc.code}.")
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HermesLlmError(f"Hermes sidecar is unavailable: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HermesLlmError("Hermes response exceeded the size limit.")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HermesLlmError("Hermes returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise HermesLlmError("Hermes response must be a JSON object.")
    return result


def _parse_intent(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    try:
        intent = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HermesLlmError(
            "Hermes did not finish with a typed room action."
        ) from exc
    if not isinstance(intent, dict) or set(intent) != {"action", "target", "query"}:
        raise HermesLlmError("Hermes intent fields did not match the schema.")
    return intent


class HermesLlmRouter(LlmRouter):
    """Ask a loopback Hermes tool loop for one validated room action."""

    backend_name = "hermes"
    error_type = HermesLlmError

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        model: str = DEFAULT_HERMES_MODEL,
        timeout: float = 25.0,
        requester: HermesRequester = request_hermes_json,
    ) -> None:
        super().__init__()
        api_key = api_key.strip()
        model = model.strip()
        if len(api_key) < 16 or any(character.isspace() for character in api_key):
            raise ValueError(
                "Hermes API key must be at least 16 non-whitespace characters."
            )
        if not model or any(character.isspace() for character in model):
            raise ValueError("Hermes API model name is invalid.")
        if timeout <= 0:
            raise ValueError("Hermes timeout must be positive.")
        self.endpoint = _validate_hermes_base_url(base_url) + "/v1/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._requester = requester

    def _completion(self, transcript: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "stream": False,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _HERMES_SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
        }
        response = self._requester(
            self.endpoint,
            payload,
            self.timeout,
            self.api_key,
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise HermesLlmError(
                "Hermes response did not contain a completion."
            ) from exc
        if not isinstance(content, str):
            raise HermesLlmError("Hermes completion was not text.")
        return _parse_intent(content)

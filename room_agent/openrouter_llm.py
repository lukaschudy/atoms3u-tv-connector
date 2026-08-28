"""Authenticated OpenRouter intent adapter with strict structured output."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from local_llm import (
    MAX_QUERY_CHARACTERS,
    MAX_RESPONSE_BYTES,
    LlmError,
    LlmRouter,
    _EXAMPLE_MESSAGES,
    _SYSTEM_PROMPT,
)


OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4-flash"
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._~:/-]{1,160}$")
_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "play_latest",
                "open",
                "search",
                "pause",
                "resume",
                "close",
                "remote",
                "unsupported",
            ],
        },
        "target": {"type": "string", "enum": ["tv", "notebook"]},
        "query": {"type": "string", "maxLength": MAX_QUERY_CHARACTERS},
    },
    "required": ["action", "target", "query"],
    "additionalProperties": False,
}


class OpenRouterLlmError(LlmError):
    pass


OpenRouterRequester = Callable[
    [str, dict[str, Any], float, str],
    dict[str, Any],
]


def request_openrouter_json(
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
            "X-OpenRouter-Title": "Room Voice Agent",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        messages = {
            401: "OpenRouter authentication failed.",
            402: "OpenRouter account has insufficient credits.",
            403: "OpenRouter rejected the request.",
            408: "OpenRouter timed out.",
            429: "OpenRouter rate limit was reached.",
            502: "The selected OpenRouter model is temporarily unavailable.",
            503: "No compatible OpenRouter provider is currently available.",
        }
        raise OpenRouterLlmError(
            messages.get(exc.code, f"OpenRouter request failed with HTTP {exc.code}.")
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OpenRouterLlmError(f"OpenRouter is unavailable: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OpenRouterLlmError("OpenRouter response exceeded the size limit.")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenRouterLlmError("OpenRouter returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise OpenRouterLlmError("OpenRouter response must be a JSON object.")
    return result


class OpenRouterLlmRouter(LlmRouter):
    """Use OpenRouter only for semantic planning; Python still executes actions."""

    backend_name = "openrouter"
    error_type = OpenRouterLlmError

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_OPENROUTER_MODEL,
        timeout: float = 4.0,
        requester: OpenRouterRequester = request_openrouter_json,
    ) -> None:
        super().__init__()
        api_key = api_key.strip()
        model = model.strip()
        if not api_key or any(character.isspace() for character in api_key):
            raise ValueError("OpenRouter API key is empty or contains whitespace.")
        if (
            not _MODEL_PATTERN.fullmatch(model)
            or "://" in model
            or model.startswith("/")
            or "//" in model
        ):
            raise ValueError("OpenRouter model slug is invalid.")
        if timeout <= 0:
            raise ValueError("OpenRouter timeout must be positive.")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._requester = requester

    def _completion(self, transcript: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 160,
            "reasoning": {"effort": "none", "exclude": True},
            "provider": {"require_parameters": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "room_action",
                    "strict": True,
                    "schema": _INTENT_SCHEMA,
                },
            },
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                *_EXAMPLE_MESSAGES,
                {"role": "user", "content": transcript},
            ],
        }
        response = self._requester(
            OPENROUTER_ENDPOINT,
            payload,
            self.timeout,
            self.api_key,
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterLlmError(
                "OpenRouter response did not contain a completion."
            ) from exc
        if not isinstance(content, str):
            raise OpenRouterLlmError("OpenRouter completion was not text.")
        try:
            intent = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenRouterLlmError(
                "OpenRouter completion was not a JSON object."
            ) from exc
        if not isinstance(intent, dict) or set(intent) != {"action", "target", "query"}:
            raise OpenRouterLlmError(
                "OpenRouter intent fields did not match the schema."
            )
        return intent

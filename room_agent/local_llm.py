"""Optional local-LLM intent adapter for broader room commands."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from intent_router import NoActivation, NoRoute, normalize, route
from protocol import ActionRequest


MAX_RESPONSE_BYTES = 16_384
MAX_QUERY_CHARACTERS = 120
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_SYSTEM_PROMPT = (
    "Parse English or Czech room commands. Return one compact JSON object with "
    "action, target, query. action is play_latest, open, search, pause, resume, close, remote, or "
    "unsupported. target is tv or notebook. Big screen and television mean tv; "
    "laptop and computer mean notebook. query contains only the show, channel, "
    "website, native TV app, person, or content title. Remove request verbs, device words, latest, "
    "newest, episode, podcast, and please. Preserve proper names. For pause, resume, "
    "or close, query is empty. For remote, target is tv and query is one canonical "
    "command: power_on, power_off, mute, home, back, select, input, navigate_up, "
    "navigate_down, navigate_left, navigate_right, volume_up, or volume_down; append "
    "a number from 1 to 20 for repeated volume or navigation, for example volume_up 10. "
    "Use search for explicit web searches and factual "
    "questions whose answer should be looked up, preserving the useful question "
    "as query. Use unsupported when no listed action applies."
)
_EXAMPLE_MESSAGES = (
    {
        "role": "user",
        "content": "Play the newest episode of All-In Podcast on the TV.",
    },
    {
        "role": "assistant",
        "content": (
            '{"action":"play_latest","target":"tv",'
            '"query":"All-In Podcast"}'
        ),
    },
    {"role": "user", "content": "Otevri YouTube na notebooku."},
    {
        "role": "assistant",
        "content": '{"action":"open","target":"notebook","query":"YouTube"}',
    },
    {"role": "user", "content": "How much does an elephant weigh?"},
    {
        "role": "assistant",
        "content": (
            '{"action":"search","target":"tv",'
            '"query":"how much does an elephant weigh"}'
        ),
    },
    {"role": "user", "content": "Could you make the television louder by 8?"},
    {
        "role": "assistant",
        "content": '{"action":"remote","target":"tv","query":"volume_up 8"}',
    },
)


class LlmError(RuntimeError):
    pass


class LocalLlmError(LlmError):
    pass


JsonRequester = Callable[[str, dict[str, Any], float], dict[str, Any]]


def _validate_loopback_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("The local LLM URL must be an unauthenticated loopback HTTP URL.")
    return base_url.rstrip("/")


def request_json(endpoint: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1_024).decode("utf-8", errors="replace")
        raise LocalLlmError(f"Local LLM rejected the request ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LocalLlmError(f"Local LLM is unavailable: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise LocalLlmError("Local LLM response exceeded the size limit.")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalLlmError("Local LLM returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise LocalLlmError("Local LLM response must be a JSON object.")
    return result


class LlmRouter:
    """Convert a provider completion into the existing typed action protocol."""

    backend_name = "llm"
    error_type = LlmError

    def __init__(self) -> None:
        self.last_backend: str | None = None

    def _completion(self, transcript: str) -> dict[str, Any]:
        raise NotImplementedError

    def route(self, transcript: str, *, active_target: str | None = None) -> ActionRequest:
        intent = self._completion(transcript)
        action = intent.get("action")
        target = intent.get("target")
        query_value = intent.get("query")
        if not all(isinstance(value, str) for value in (action, target, query_value)):
            raise self.error_type("LLM intent values must be strings.")

        action = action.strip().casefold()
        target = target.strip().casefold()
        query = " ".join(query_value.split())
        if target not in {"tv", "notebook"}:
            raise self.error_type("LLM returned an unsupported target.")
        if len(query) > MAX_QUERY_CHARACTERS:
            raise self.error_type("LLM query exceeded the length limit.")

        if action == "unsupported":
            raise self.error_type("No supported room action matched the transcript.")
        if action == "remote":
            if target != "tv":
                raise self.error_type("TV remote actions must target the television.")
            match = re.fullmatch(
                r"(power_on|power_off|mute|home|back|select|input|"
                r"navigate_up|navigate_down|navigate_left|navigate_right|"
                r"volume_up|volume_down)(?:\s+([0-9]{1,2}))?",
                query.casefold(),
            )
            if match is None:
                raise self.error_type("LLM returned an unsupported remote command.")
            command = match.group(1)
            count = int(match.group(2) or "1")
            if not 1 <= count <= 20:
                raise self.error_type("LLM remote repetition count is outside the limit.")
            result = ActionRequest.create(
                "tv.remote", "tv", {"command": command, "count": count}
            )
        elif action in {"pause", "resume", "close"}:
            if active_target not in {"tv", "notebook"}:
                raise self.error_type(
                    "There is no active playback session to control."
                )
            typed_action = {
                "pause": "media.pause",
                "resume": "media.resume",
                "close": "media.close",
            }[action]
            result = ActionRequest.create(typed_action, active_target, {})
        elif action == "open":
            normalized_query = normalize(query)
            site = {
                "youtube": "youtube",
                "miton": "miton",
                "miton cz": "miton",
            }.get(normalized_query)
            if site is not None:
                result = ActionRequest.create("web.open", target, {"site": site})
            elif target == "tv":
                app = normalized_query.replace(" ", "_")
                if not app or len(app) > 48:
                    raise self.error_type("The requested native TV app name is invalid.")
                result = ActionRequest.create("app.open", "tv", {"app": app})
            else:
                raise self.error_type("The requested website is not registered.")
        elif action == "search":
            if not query:
                raise self.error_type("The Google search query was empty.")
            result = ActionRequest.create(
                "web.search",
                target,
                {"provider": "google", "query": query},
            )
        elif action != "play_latest" or not query:
            raise self.error_type("LLM returned an unsupported action or empty query.")
        elif normalize(query) in {"cinkatko", "miton", "miton cz"}:
            result = ActionRequest.create(
                "youtube.play_latest",
                target,
                {"channel": "cinkatko", "selector": "latest_episode"},
            )
        else:
            result = ActionRequest.create(
                "youtube.search_latest",
                target,
                {"query": query, "selector": "latest_episode"},
            )
        self.last_backend = self.backend_name
        return result


class LocalLlmRouter(LlmRouter):
    """Map free-form text through a loopback llama.cpp server."""

    backend_name = "local"
    error_type = LocalLlmError

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 8.0,
        requester: JsonRequester = request_json,
    ) -> None:
        super().__init__()
        if timeout <= 0:
            raise ValueError("Local LLM timeout must be positive.")
        self.endpoint = _validate_loopback_base_url(base_url) + "/v1/chat/completions"
        self.timeout = timeout
        self._requester = requester

    def _completion(self, transcript: str) -> dict[str, Any]:
        payload = {
            "model": "local",
            "temperature": 0,
            "max_tokens": 40,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                *_EXAMPLE_MESSAGES,
                {"role": "user", "content": transcript},
            ],
        }
        response = self._requester(self.endpoint, payload, self.timeout)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LocalLlmError("Local LLM response did not contain a completion.") from exc
        if not isinstance(content, str):
            raise LocalLlmError("Local LLM completion was not text.")
        try:
            intent = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LocalLlmError("Local LLM completion was not a JSON object.") from exc
        if not isinstance(intent, dict) or set(intent) != {"action", "target", "query"}:
            raise LocalLlmError("Local LLM intent fields did not match the schema.")
        return intent



class FallbackLlmRouter(LlmRouter):
    """Try semantic routers in order while keeping failures bounded."""

    def __init__(self, routers: tuple[LlmRouter, ...]) -> None:
        super().__init__()
        if not routers:
            raise ValueError("At least one semantic router is required.")
        self.routers = routers

    def route(self, transcript: str, *, active_target: str | None = None) -> ActionRequest:
        errors = []
        self.last_backend = None
        for router in self.routers:
            try:
                result = router.route(transcript, active_target=active_target)
            except LlmError as exc:
                errors.append(f"{router.backend_name}: {exc}")
                continue
            self.last_backend = router.last_backend or router.backend_name
            return result
        raise LlmError("; ".join(errors))


def hybrid_route(
    transcript: str,
    *,
    active_target: str | None = None,
    active_surface: str | None = None,
    require_activation: bool = False,
    default_open_target: str = "notebook",
    llm_router: LlmRouter | None = None,
) -> ActionRequest:
    """Prefer the deterministic router and use the local model only for no-match."""

    try:
        return route(
            transcript,
            active_target=active_target,
            active_surface=active_surface,
            require_activation=require_activation,
            default_open_target=default_open_target,
        )
    except NoActivation:
        raise
    except NoRoute as exc:
        if llm_router is None or not exc.allow_llm_fallback:
            raise
        try:
            return llm_router.route(transcript, active_target=active_target)
        except LlmError as llm_exc:
            raise NoRoute(str(llm_exc)) from llm_exc

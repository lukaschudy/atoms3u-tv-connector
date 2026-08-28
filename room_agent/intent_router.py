"""Deterministic MVP router. A schema-based reasoning layer can replace it later."""

from __future__ import annotations

import re
import unicodedata

from protocol import ActionRequest


class NoRoute(ValueError):
    def __init__(self, message: str, *, allow_llm_fallback: bool = True) -> None:
        super().__init__(message)
        self.allow_llm_fallback = allow_llm_fallback


class NoActivation(NoRoute):
    pass


MAX_WEB_QUERY_CHARACTERS = 120
PAUSE_SESSION_COMMANDS = {
    "pause",
    "pause video",
    "pause the video",
    "pause playback",
    "pause the playback",
    "pause the video please",
    "please pause",
    "please pause video",
    "please pause the video",
}
RESUME_SESSION_COMMANDS = {
    "continue",
    "resume",
    "continue video",
    "continue the video",
    "continue playback",
    "continue the playback",
    "resume video",
    "resume the video",
    "resume playback",
    "resume the playback",
    "continue the video please",
    "resume the video please",
    "please continue",
    "please continue the video",
    "please resume",
    "please resume the video",
}
INFORMATION_REQUEST_PREFIXES = (
    "what ",
    "what s ",
    "whats ",
    "who ",
    "who s ",
    "where ",
    "where s ",
    "when ",
    "when s ",
    "why ",
    "how ",
    "which ",
    "tell me ",
    "please tell me ",
    "can you tell me ",
    "find out ",
    "look up ",
    "search for ",
)
_TARGET_SUFFIX = re.compile(
    r"\s+on\s+(?:the\s+)?"
    r"(?P<target>tv|television|laptop|notebook|computer|pc)$",
    flags=re.IGNORECASE,
)
_CZECH_TARGET_SUFFIX = re.compile(
    r"\s+na\s+(?P<target>tv|televizi|obrazovce|notebooku|laptopu|počítači|pocitaci)$",
    flags=re.IGNORECASE,
)
_LATEST_PODCAST_PATTERNS = (
    re.compile(
        r"^(?:please\s+)?(?:play|start|open|show\s+me|put\s+on)\s+"
        r"(?:the\s+)?(?:latest|newest|most\s+recent)\s+"
        r"(?:podcast|episode)\s+(?:of|from)\s+(?P<query>.+)$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^(?:please\s+)?(?:play|start|open|show\s+me|put\s+on)\s+"
        r"(?:the\s+)?(?:latest|newest|most\s+recent)\s+"
        r"(?P<query>.+?)\s+(?:podcast|episode)$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^(?:please\s+)?(?:play|start|open|show\s+me|put\s+on)\s+"
        r"(?P<query>.+?)(?:['’]s)\s+(?:latest|newest|most\s+recent)\s+"
        r"(?:podcast|episode)$",
        flags=re.IGNORECASE,
    ),
)

_REMOTE_SIMPLE_COMMANDS = {
    "mute": "mute",
    "mute tv": "mute",
    "mute the tv": "mute",
    "unmute": "mute",
    "unmute tv": "mute",
    "ztlum televizi": "mute",
    "zapni zvuk": "mute",
    "home": "home",
    "go home": "home",
    "home screen": "home",
    "domu": "home",
    "back": "back",
    "go back": "back",
    "zpet": "back",
    "ok": "select",
    "select": "select",
    "confirm": "select",
    "enter": "select",
    "potvrd": "select",
    "input": "input",
    "source": "input",
    "change input": "input",
    "change source": "input",
    "zmen vstup": "input",
}
_REMOTE_DIRECTIONS = {
    "up": "up",
    "move up": "up",
    "go up": "up",
    "nahoru": "up",
    "down": "down",
    "move down": "down",
    "go down": "down",
    "dolu": "down",
    "left": "left",
    "move left": "left",
    "go left": "left",
    "doleva": "left",
    "right": "right",
    "move right": "right",
    "go right": "right",
    "doprava": "right",
}


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def extract_target_suffix(text: str) -> tuple[str, str | None]:
    """Remove an optional English or Czech device suffix from a command."""

    for pattern in (_TARGET_SUFFIX, _CZECH_TARGET_SUFFIX):
        match = pattern.search(text)
        if match is None:
            continue
        normalized_target = normalize(match.group("target"))
        target = (
            "tv"
            if normalized_target in {"tv", "television", "televizi", "obrazovce"}
            else "notebook"
        )
        return text[: match.start()].strip(" .?!"), target
    return text, None


def google_search_query(text: str) -> str | None:
    compact = " ".join(text.split())
    match = re.fullmatch(
        r"(?:google|search\s+google\s+for)\s+(.+)",
        compact,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    query = re.sub(
        r"\s+on\s+(?:the\s+)?(?:tv|television|laptop|notebook|computer|pc)$",
        "",
        match.group(1),
        flags=re.IGNORECASE,
    ).strip(" .?!")
    return query or None


def information_search_request(text: str) -> tuple[str, str | None] | None:
    """Extract a general factual lookup without encoding topic-specific phrases."""

    query = " ".join(text.split()).strip(" .?!")
    normalized = normalize(query)
    if not any(normalized.startswith(prefix) for prefix in INFORMATION_REQUEST_PREFIXES):
        return None

    query, target = extract_target_suffix(query)
    return (query, target) if query else None


def latest_podcast_request(text: str) -> tuple[str, str | None] | None:
    """Extract a podcast title from common play-the-latest phrasing."""

    command = " ".join(text.split()).strip(" .?!")
    command, target = extract_target_suffix(command)
    command = re.sub(
        r"\s+on\s+(?:youtube|you\s+tube)$",
        "",
        command,
        flags=re.IGNORECASE,
    ).strip(" .?!")

    for pattern in _LATEST_PODCAST_PATTERNS:
        match = pattern.fullmatch(command)
        if match is None:
            continue
        query = match.group("query").strip(" .?!\"'’")
        query = re.sub(
            r"\s+(?:podcast|show)$",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip()
        if query and normalize(query) not in {"podcast", "episode", "show"}:
            return query, target
    return None


def youtube_play_request(text: str) -> tuple[str, str | None] | None:
    """Extract a bounded free-form video query from a YouTube follow-up."""

    command = " ".join(text.split()).strip(" .?!")
    command = re.sub(
        r"^(?:(?:room|voice)\s+(?:agent|assistant))[,\s:;-]+",
        "",
        command,
        flags=re.IGNORECASE,
    ).strip(" .?!")
    command, target = extract_target_suffix(command)
    command = re.sub(
        r"\s+on\s+(?:youtube|you\s+tube)$",
        "",
        command,
        flags=re.IGNORECASE,
    ).strip(" .?!")
    match = re.fullmatch(
        r"(?:(?:could|can|would)\s+you\s+)?(?:please\s+|prosim\s+)?"
        r"(?:play|start|put\s+on|pust|pusť|prehraj|přehraj|spust|spusť)\s+(.+?)"
        r"(?:\s+(?:please|prosim))?",
        command,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    query = match.group(1).strip(" .?!\"'")
    vague_queries = {
        "a video",
        "anything",
        "it",
        "music",
        "something",
        "that",
        "the video",
        "video",
        "whatever",
        "this",
    }
    if not query or normalize(query) in vague_queries:
        raise NoRoute(
            "Tell me what you want to play on YouTube.",
            allow_llm_fallback=False,
        )
    return query, target


def remote_control_request(text: str) -> tuple[str, int] | None:
    """Parse common bounded TV-remote phrases without exposing raw key codes."""

    command = normalize(text)
    command = re.sub(r"^(?:room|voice) (?:agent|assistant) ", "", command)
    command = re.sub(r"^(?:please|prosim) ", "", command)
    command = re.sub(r" (?:please|prosim)$", "", command)
    command = re.sub(r" on (?:the )?(?:tv|television)$", "", command)

    power_patterns = (
        (r"^(?:turn|switch|power) (?:the )?(?:tv|television) off$", "power_off"),
        (r"^(?:turn|switch|power) off (?:the )?(?:tv|television)$", "power_off"),
        (r"^vypni (?:televizi|tv)$", "power_off"),
        (r"^(?:turn|switch|power) (?:the )?(?:tv|television) on$", "power_on"),
        (r"^(?:turn|switch|power) on (?:the )?(?:tv|television)$", "power_on"),
        (r"^zapni (?:televizi|tv)$", "power_on"),
    )
    for pattern, action in power_patterns:
        if re.fullmatch(pattern, command):
            return action, 1

    count = 1
    count_match = re.search(r"(?: by| o)? (\d{1,2})(?: times| steps| krat)?$", command)
    if count_match is not None:
        count = int(count_match.group(1))
        command = command[: count_match.start()].strip()
        if not 1 <= count <= 20:
            raise NoRoute(
                "TV remote repetitions must be between 1 and 20.",
                allow_llm_fallback=False,
            )

    volume_up = {
        "volume up",
        "turn up volume",
        "turn the volume up",
        "increase volume",
        "increase the volume",
        "raise volume",
        "raise the volume",
        "zesil hlasitost",
        "hlasitost nahoru",
    }
    volume_down = {
        "volume down",
        "turn down volume",
        "turn the volume down",
        "decrease volume",
        "decrease the volume",
        "lower volume",
        "lower the volume",
        "sniz hlasitost",
        "hlasitost dolu",
    }
    if command in volume_up:
        return "volume_up", count
    if command in volume_down:
        return "volume_down", count
    if count != 1:
        return None
    if command in _REMOTE_SIMPLE_COMMANDS:
        return _REMOTE_SIMPLE_COMMANDS[command], 1
    if command in _REMOTE_DIRECTIONS:
        return f"navigate_{_REMOTE_DIRECTIONS[command]}", 1
    return None


def route(
    transcript: str,
    *,
    active_target: str | None = None,
    active_surface: str | None = None,
    require_activation: bool = False,
    default_open_target: str = "notebook",
) -> ActionRequest:
    if default_open_target not in {"notebook", "tv"}:
        raise ValueError("Default open target must be notebook or tv.")
    normalized = normalize(transcript)
    words = set(normalized.split())
    open_words = {
        "open",
        "launch",
        "start",
        "play",
        "plays",
        "otevri",
        "spust",
        "pust",
        "prehraj",
    }
    laptop_words = {"laptop", "notebook", "computer", "pc", "pocitac"}
    tv_words = {"tv", "television", "televize", "televizi", "obrazovka", "obrazovce"}
    cinkatko_words = {"cinkatko", "cinkatka", "miton", "mitonu"}
    content_words = {"podcast", "episode", "dil", "dilu", "latest", "newest", "posledni"}
    negation_words = {"not", "dont", "ne", "nepoustej", "nespoustej", "neotvirej"}
    activated = (
        ("room" in words and bool(words.intersection({"agent", "agents", "assistant"})))
        or ("voice" in words and bool(words.intersection({"agent", "assistant"})))
    )
    close_requested = normalized in {
        "quit",
        "close tab",
        "close the tab",
        "room agent quit",
        "room agent close tab",
        "room agent close the tab",
        "zavri kartu",
        "room agent zavri kartu",
    }
    first_result_requested = normalized in {
        "open first page",
        "open the first page",
        "open first result",
        "open the first result",
        "open first one",
        "open the first one",
    }
    bare_session_control = active_target in {"tv", "notebook"} and (
        normalized in PAUSE_SESSION_COMMANDS
        or normalized in RESUME_SESSION_COMMANDS
        or first_result_requested
    )

    if words.intersection(negation_words):
        raise NoRoute("Negated commands are not executed.", allow_llm_fallback=False)

    remote_request = remote_control_request(transcript)
    if remote_request is not None:
        command, count = remote_request
        if require_activation and not activated:
            raise NoActivation("The Room Agent activation phrase was not heard.")
        return ActionRequest.create(
            action="tv.remote",
            target="tv",
            arguments={"command": command, "count": count},
        )

    if close_requested:
        if active_target not in {"tv", "notebook"} and not activated:
            raise NoActivation(
                "Quit and Close require an active media session or Room Agent phrase."
            )
        return ActionRequest.create(
            action="media.close",
            target=active_target or default_open_target,
            arguments={},
        )

    if require_activation and not activated and not bare_session_control:
        raise NoActivation("The Room Agent activation phrase was not heard.")

    if first_result_requested:
        if active_target not in {"tv", "notebook"}:
            raise NoRoute(
                "There is no active search whose first result can be opened.",
                allow_llm_fallback=False,
            )
        return ActionRequest.create(
            action="web.open_first_result",
            target=active_target,
            arguments={},
        )

    if words.intersection({"pause", "pozastav", "pozastavit"}):
        if active_target not in {"tv", "notebook"}:
            raise NoRoute(
                "There is no active playback session to pause.",
                allow_llm_fallback=False,
            )
        return ActionRequest.create(
            action="media.pause",
            target=active_target,
            arguments={},
        )

    if words.intersection({"continue", "resume", "pokracuj", "pokracovat"}):
        if active_target not in {"tv", "notebook"}:
            raise NoRoute(
                "There is no active playback session to continue.",
                allow_llm_fallback=False,
            )
        return ActionRequest.create(
            action="media.resume",
            target=active_target,
            arguments={},
        )

    search_query = google_search_query(transcript)
    if search_query is not None:
        if len(search_query) > MAX_WEB_QUERY_CHARACTERS:
            raise NoRoute(
                "The Google search query is too long.",
                allow_llm_fallback=False,
            )
        if words.intersection(laptop_words) and words.intersection(tv_words):
            raise NoRoute(
                "The command names both the laptop and TV.",
                allow_llm_fallback=False,
            )
        if words.intersection(tv_words):
            target = "tv"
        elif words.intersection(laptop_words):
            target = "notebook"
        else:
            target = default_open_target
        return ActionRequest.create(
            action="web.search",
            target=target,
            arguments={"provider": "google", "query": search_query},
        )

    information_request = information_search_request(transcript)
    if information_request is not None:
        query, explicit_target = information_request
        if len(query) > MAX_WEB_QUERY_CHARACTERS:
            raise NoRoute(
                "The informational search query is too long.",
                allow_llm_fallback=False,
            )
        if words.intersection(laptop_words) and words.intersection(tv_words):
            raise NoRoute(
                "The command names both the laptop and TV.",
                allow_llm_fallback=False,
            )
        return ActionRequest.create(
            action="web.search",
            target=explicit_target or default_open_target,
            arguments={"provider": "google", "query": query},
        )

    podcast_request = latest_podcast_request(transcript)
    if podcast_request is not None:
        query, explicit_target = podcast_request
        if len(query) > MAX_WEB_QUERY_CHARACTERS:
            raise NoRoute(
                "The podcast search query is too long.",
                allow_llm_fallback=False,
            )
        if words.intersection(laptop_words) and words.intersection(tv_words):
            raise NoRoute(
                "The command names both the laptop and TV.",
                allow_llm_fallback=False,
            )
        target = explicit_target or active_target or default_open_target
        if normalize(query) in {"cinkatko", "miton", "miton cz"}:
            return ActionRequest.create(
                action="youtube.play_latest",
                target=target,
                arguments={"channel": "cinkatko", "selector": "latest_episode"},
            )
        return ActionRequest.create(
            action="youtube.search_latest",
            target=target,
            arguments={"query": query, "selector": "latest_episode"},
        )

    requested_site = None
    if "youtube" in words and not words.intersection(content_words):
        requested_site = "youtube"
    elif "miton" in words and not words.intersection(content_words):
        requested_site = "miton"

    requested_app = "netflix" if "netflix" in words else None
    if words.intersection(open_words) and requested_app is not None:
        if words.intersection(laptop_words) and words.intersection(tv_words):
            raise NoRoute(
                "The command names both the laptop and TV.",
                allow_llm_fallback=False,
            )
        if words.intersection(laptop_words):
            raise NoRoute(
                "Native TV apps cannot be launched on the notebook companion.",
                allow_llm_fallback=False,
            )
        return ActionRequest.create(
            action="app.open",
            target="tv",
            arguments={"app": requested_app},
        )

    if words.intersection(open_words) and requested_site is not None:
        if words.intersection(laptop_words) and words.intersection(tv_words):
            raise NoRoute(
                "The command names both the laptop and TV.",
                allow_llm_fallback=False,
            )
        if words.intersection(tv_words):
            target = "tv"
        elif words.intersection(laptop_words):
            target = "notebook"
        else:
            target = default_open_target
        return ActionRequest.create(
            action="web.open",
            target=target,
            arguments={"site": requested_site},
        )

    if words.intersection(open_words) and words.intersection(cinkatko_words):
        if words.intersection(laptop_words) and words.intersection(tv_words):
            raise NoRoute(
                "The command names both the laptop and TV.",
                allow_llm_fallback=False,
            )
        target = (
            "tv"
            if words.intersection(tv_words)
            else active_target or default_open_target
        )
        return ActionRequest.create(
            action="youtube.play_latest",
            target=target,
            arguments={"channel": "cinkatko", "selector": "latest_episode"},
        )

    youtube_request = youtube_play_request(transcript)
    if youtube_request is not None and active_surface == "youtube":
        query, explicit_target = youtube_request
        if words.intersection(laptop_words) and words.intersection(tv_words):
            raise NoRoute(
                "The command names both the laptop and TV.",
                allow_llm_fallback=False,
            )
        if len(query) > MAX_WEB_QUERY_CHARACTERS:
            raise NoRoute(
                "The YouTube search query is too long.",
                allow_llm_fallback=False,
            )
        return ActionRequest.create(
            action="youtube.search_play",
            target=explicit_target or active_target or default_open_target,
            arguments={"query": query},
        )

    app_match = re.fullmatch(
        r"(?:please\s+)?(?:open|launch|start)\s+(?:the\s+)?"
        r"(?P<app>[a-z0-9][a-z0-9 .&+_-]{0,60}?)"
        r"(?:\s+app)?(?:\s+on\s+(?:the\s+)?(?:tv|television))?",
        transcript.strip(" .?!").casefold(),
    )
    if app_match is not None and not words.intersection(laptop_words):
        app = normalize(app_match.group("app")).replace(" ", "_")
        if app and len(app) <= 48:
            return ActionRequest.create(
                action="app.open",
                target="tv",
                arguments={"app": app},
            )
    if youtube_request is not None:
        raise NoRoute(
            "Open YouTube before asking it to play a specific video.",
            allow_llm_fallback=False,
        )
    raise NoRoute("No registered action matched the transcript.")

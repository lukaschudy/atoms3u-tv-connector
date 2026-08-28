"""Resolve newest eligible YouTube episodes from a channel or search title."""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable


YOUTUBE_API_ROOT = "https://www.googleapis.com/youtube/v3"
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
MAX_SEARCH_QUERY_CHARACTERS = 120
DEFAULT_PODCAST_CATALOG = Path(__file__).with_name("podcasts.json")
_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_DERIVATIVE_CHANNEL_WORDS = {
    "clip",
    "clips",
    "fan",
    "fans",
    "highlight",
    "highlights",
    "scene",
    "scenes",
    "short",
    "shorts",
}
_GENERIC_DISCOVERY_WORDS = {
    "a",
    "an",
    "and",
    "episode",
    "episodes",
    "latest",
    "newest",
    "of",
    "official",
    "podcast",
    "show",
    "the",
}
_PODCAST_CONTEXT_WORDS = {
    "episode",
    "episodes",
    "host",
    "hosted",
    "hosts",
    "podcast",
    "show",
    "weekly",
}


class YouTubeResolverError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaItem:
    video_id: str
    title: str
    published_at: str
    duration_seconds: int
    source: str = "youtube_api"
    channel_id: str = ""
    channel_title: str = ""

    def __post_init__(self) -> None:
        if not VIDEO_ID_PATTERN.fullmatch(self.video_id):
            raise YouTubeResolverError("YouTube returned an invalid video ID.")
        if self.channel_id and not CHANNEL_ID_PATTERN.fullmatch(self.channel_id):
            raise YouTubeResolverError("YouTube returned an invalid channel ID.")

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}&autoplay=1"


@dataclass(frozen=True)
class PodcastSource:
    source_id: str
    title: str
    youtube_channel_id: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class PodcastCatalog:
    sources_by_id: dict[str, PodcastSource]
    sources_by_alias: dict[str, PodcastSource]
    ambiguous_aliases: dict[str, tuple[str, ...]]

    @classmethod
    def empty(cls) -> "PodcastCatalog":
        return cls({}, {}, {})

    def lookup(self, query: str) -> PodcastSource | None:
        return self.sources_by_alias.get(_catalog_key(query))

    def choices(self, query: str) -> tuple[PodcastSource, ...]:
        source_ids = self.ambiguous_aliases.get(_catalog_key(query), ())
        return tuple(self.sources_by_id[source_id] for source_id in source_ids)


def _catalog_key(value: str) -> str:
    tokens = re.findall(r"\w+", value.casefold())
    specific = [token for token in tokens if token not in _GENERIC_DISCOVERY_WORDS]
    return " ".join(specific or tokens)


@lru_cache(maxsize=4)
def load_podcast_catalog(path: Path = DEFAULT_PODCAST_CATALOG) -> PodcastCatalog:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise YouTubeResolverError(f"Podcast catalog could not be loaded: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "podcasts",
        "ambiguous_aliases",
    }:
        raise YouTubeResolverError("Podcast catalog fields are invalid.")
    if payload["version"] != 1 or not isinstance(payload["podcasts"], list):
        raise YouTubeResolverError("Podcast catalog version or entries are invalid.")

    sources_by_id: dict[str, PodcastSource] = {}
    sources_by_alias: dict[str, PodcastSource] = {}
    for raw_source in payload["podcasts"]:
        if not isinstance(raw_source, dict) or set(raw_source) != {
            "id",
            "title",
            "youtube_channel_id",
            "aliases",
        }:
            raise YouTubeResolverError("A podcast catalog entry is invalid.")
        source_id = raw_source["id"]
        title = raw_source["title"]
        channel_id = raw_source["youtube_channel_id"]
        aliases = raw_source["aliases"]
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"[a-z0-9_]+", source_id)
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(channel_id, str)
            or not CHANNEL_ID_PATTERN.fullmatch(channel_id)
            or not isinstance(aliases, list)
            or not aliases
            or not all(isinstance(alias, str) and _catalog_key(alias) for alias in aliases)
        ):
            raise YouTubeResolverError("A podcast catalog value is invalid.")
        if source_id in sources_by_id:
            raise YouTubeResolverError("Podcast catalog IDs must be unique.")
        source = PodcastSource(
            source_id=source_id,
            title=title.strip(),
            youtube_channel_id=channel_id,
            aliases=tuple(aliases),
        )
        sources_by_id[source_id] = source
        for alias in source.aliases:
            key = _catalog_key(alias)
            if key in sources_by_alias:
                raise YouTubeResolverError("Podcast catalog aliases must be unique.")
            sources_by_alias[key] = source

    raw_ambiguous = payload["ambiguous_aliases"]
    if not isinstance(raw_ambiguous, dict):
        raise YouTubeResolverError("Podcast catalog ambiguous aliases are invalid.")
    ambiguous_aliases: dict[str, tuple[str, ...]] = {}
    for alias, source_ids in raw_ambiguous.items():
        key = _catalog_key(alias) if isinstance(alias, str) else ""
        if (
            not key
            or key in sources_by_alias
            or not isinstance(source_ids, list)
            or len(source_ids) < 2
            or not all(
                isinstance(source_id, str) and source_id in sources_by_id
                for source_id in source_ids
            )
        ):
            raise YouTubeResolverError("A podcast catalog ambiguity is invalid.")
        ambiguous_aliases[key] = tuple(source_ids)

    return PodcastCatalog(sources_by_id, sources_by_alias, ambiguous_aliases)


def parse_iso8601_duration(value: str) -> int:
    match = _DURATION_PATTERN.fullmatch(value)
    if not match:
        raise YouTubeResolverError(f"Unsupported YouTube duration: {value!r}")
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    return (
        parts["days"] * 86_400
        + parts["hours"] * 3_600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _published_sort_value(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def select_latest_episode(
    videos: Iterable[dict[str, Any]], min_duration_seconds: int
) -> MediaItem:
    if min_duration_seconds < 1:
        raise ValueError("Minimum episode duration must be positive.")

    eligible: list[MediaItem] = []
    for video in videos:
        status = video.get("status", {})
        snippet = video.get("snippet", {})
        details = video.get("contentDetails", {})
        video_id = str(video.get("id", ""))
        duration_text = str(details.get("duration", ""))

        if not VIDEO_ID_PATTERN.fullmatch(video_id) or not duration_text:
            continue
        if status.get("privacyStatus") != "public" or status.get("embeddable") is False:
            continue
        if snippet.get("liveBroadcastContent") in {"live", "upcoming"}:
            continue

        try:
            duration = parse_iso8601_duration(duration_text)
        except YouTubeResolverError:
            continue
        if duration < min_duration_seconds:
            continue

        eligible.append(
            MediaItem(
                video_id=video_id,
                title=str(snippet.get("title", "Untitled video")),
                published_at=str(snippet.get("publishedAt", "")),
                duration_seconds=duration,
                channel_id=(
                    str(snippet.get("channelId", ""))
                    if CHANNEL_ID_PATTERN.fullmatch(
                        str(snippet.get("channelId", ""))
                    )
                    else ""
                ),
                channel_title=str(snippet.get("channelTitle", "")),
            )
        )

    if not eligible:
        minutes = min_duration_seconds // 60
        raise YouTubeResolverError(
            f"No public, playable episode of at least {minutes} minutes was found."
        )
    return max(eligible, key=lambda item: _published_sort_value(item.published_at))


def youtube_api_get(resource: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{YOUTUBE_API_ROOT}/{resource}?{query}",
        headers={"Accept": "application/json", "User-Agent": "RoomVoiceAgent/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise YouTubeResolverError(f"YouTube metadata request failed: {exc}") from exc

    if not isinstance(payload, dict):
        raise YouTubeResolverError("YouTube API returned an invalid response.")
    if "error" in payload:
        raise YouTubeResolverError("YouTube API rejected the metadata request.")
    return payload


def _resolve_uploads_playlist(
    api_key: str,
    uploads_playlist: str,
    *,
    min_duration_seconds: int,
    get_json: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> MediaItem:
    uploads_payload = get_json(
        "playlistItems",
        {
            "part": "contentDetails",
            "playlistId": uploads_playlist,
            "maxResults": 25,
            "key": api_key,
        },
    )
    video_ids = [
        item.get("contentDetails", {}).get("videoId")
        for item in uploads_payload.get("items", [])
        if isinstance(item, dict)
    ]
    video_ids = [
        str(video_id)
        for video_id in video_ids
        if VIDEO_ID_PATTERN.fullmatch(str(video_id))
    ]
    if not video_ids:
        raise YouTubeResolverError("No public uploads were returned for the channel.")

    videos_payload = get_json(
        "videos",
        {
            "part": "contentDetails,snippet,status",
            "id": ",".join(video_ids),
            "key": api_key,
        },
    )
    return select_latest_episode(
        videos_payload.get("items", []),
        min_duration_seconds=min_duration_seconds,
    )


def _resolve_catalog_source(
    api_key: str,
    source: PodcastSource,
    *,
    min_duration_seconds: int,
    get_json: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> MediaItem:
    channel_payload = get_json(
        "channels",
        {
            "part": "contentDetails,snippet",
            "id": source.youtube_channel_id,
            "key": api_key,
        },
    )
    channels = channel_payload.get("items", [])
    if not isinstance(channels, list) or len(channels) != 1:
        raise YouTubeResolverError(
            f"The configured {source.title} channel is unavailable."
        )
    try:
        uploads_playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except (KeyError, TypeError) as exc:
        raise YouTubeResolverError(
            f"The configured {source.title} channel has no uploads playlist."
        ) from exc
    return _resolve_uploads_playlist(
        api_key,
        str(uploads_playlist),
        min_duration_seconds=min_duration_seconds,
        get_json=get_json,
    )


def _channel_match_score(
    query: str,
    title: str,
    description: str,
    rank: int,
    subscriber_count: int,
) -> float | None:
    """Rank textually relevant podcast channels without popularity taking over."""

    query_normalized = " ".join(re.findall(r"\w+", query.casefold()))
    title_normalized = " ".join(re.findall(r"\w+", title.casefold()))
    description_normalized = " ".join(
        re.findall(r"\w+", description.casefold())
    )
    query_tokens = set(query_normalized.split()) - _GENERIC_DISCOVERY_WORDS
    if not query_tokens:
        query_tokens = set(query_normalized.split())
    title_tokens = set(title_normalized.split())
    description_tokens = set(description_normalized.split())
    title_overlap = len(query_tokens & title_tokens)
    description_overlap = len(query_tokens & description_tokens)
    combined_overlap = len(query_tokens & (title_tokens | description_tokens))
    coverage = combined_overlap / max(len(query_tokens), 1)
    if combined_overlap == 0 or coverage < 0.5:
        return None

    exact = int(bool(query_normalized) and query_normalized == title_normalized)
    contained = int(bool(query_normalized) and query_normalized in title_normalized)
    derivative = int(bool(title_tokens & _DERIVATIVE_CHANNEL_WORDS))
    host_relationship = int(
        query_tokens <= description_tokens
        and bool(description_tokens & {"host", "hosted", "hosts"})
    )
    podcast_context = int(
        bool((title_tokens | description_tokens) & _PODCAST_CONTEXT_WORDS)
    )
    popularity = math.log10(max(subscriber_count, 0) + 1) * 100
    return (
        popularity
        + exact * 150
        + contained * 50
        + coverage * 200
        + title_overlap / max(len(query_tokens), 1) * 200
        + description_overlap / max(len(query_tokens), 1) * 160
        + host_relationship * 350
        + podcast_context * 40
        - derivative * 400
        - rank * 5
    )


def resolve_latest_episode(
    api_key: str,
    *,
    handle: str = "@mitoncz",
    min_duration_seconds: int = 600,
    get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = youtube_api_get,
) -> MediaItem:
    if not api_key:
        raise YouTubeResolverError("YOUTUBE_API_KEY is not configured.")
    if handle != "@mitoncz":
        raise YouTubeResolverError("Only the configured Cinkatko channel is allowed.")

    channel_payload = get_json(
        "channels",
        {
            "part": "contentDetails,snippet",
            "forHandle": handle,
            "key": api_key,
        },
    )
    channels = channel_payload.get("items", [])
    if not isinstance(channels, list) or len(channels) != 1:
        raise YouTubeResolverError(
            f"Expected one channel for {handle}, received {len(channels)}."
        )

    try:
        uploads_playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except (KeyError, TypeError) as exc:
        raise YouTubeResolverError(
            f"The uploads playlist was missing for {handle}."
        ) from exc

    return _resolve_uploads_playlist(
        api_key,
        str(uploads_playlist),
        min_duration_seconds=min_duration_seconds,
        get_json=get_json,
    )


def resolve_latest_search(
    api_key: str,
    query: str,
    *,
    min_duration_seconds: int = 600,
    get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = youtube_api_get,
    catalog: PodcastCatalog | None = None,
) -> MediaItem:
    """Resolve a podcast channel first, then its newest long-form public upload."""

    if not api_key:
        raise YouTubeResolverError("YOUTUBE_API_KEY is not configured.")
    query = " ".join(query.split())
    if not query or len(query) > MAX_SEARCH_QUERY_CHARACTERS:
        raise YouTubeResolverError("YouTube search query has an invalid length.")

    catalog = load_podcast_catalog() if catalog is None else catalog
    choices = catalog.choices(query)
    if choices:
        titles = ", ".join(source.title for source in choices)
        raise YouTubeResolverError(
            f"{query!r} matches multiple podcasts: {titles}. Say the podcast title."
        )
    known_source = catalog.lookup(query)
    if known_source is not None:
        return _resolve_catalog_source(
            api_key,
            known_source,
            min_duration_seconds=min_duration_seconds,
            get_json=get_json,
        )

    discovery_query = query
    if "podcast" not in re.findall(r"\w+", query.casefold()):
        discovery_query = f"{query} podcast"

    search_payload = get_json(
        "search",
        {
            "part": "snippet",
            "q": discovery_query,
            "type": "channel",
            "order": "relevance",
            "maxResults": 5,
            "safeSearch": "moderate",
            "key": api_key,
        },
    )
    candidates: list[tuple[str, str, int]] = []
    for rank, item in enumerate(search_payload.get("items", [])):
        if not isinstance(item, dict) or not isinstance(item.get("id"), dict):
            continue
        channel_id = str(item["id"].get("channelId", ""))
        title = str(item.get("snippet", {}).get("title", ""))
        if CHANNEL_ID_PATTERN.fullmatch(channel_id):
            candidates.append((channel_id, title, rank))
    if not candidates:
        raise YouTubeResolverError(f"No YouTube channel was returned for {query!r}.")

    candidate_ids = [candidate[0] for candidate in candidates]
    channel_payload = get_json(
        "channels",
        {
            "part": "contentDetails,snippet,statistics",
            "id": ",".join(candidate_ids),
            "key": api_key,
        },
    )
    channels = channel_payload.get("items", [])
    if not isinstance(channels, list) or not channels:
        raise YouTubeResolverError("The selected YouTube channel was unavailable.")
    channel_by_id = {
        str(channel.get("id", "")): channel
        for channel in channels
        if isinstance(channel, dict)
    }
    ranked_channels: list[tuple[float, dict[str, Any]]] = []
    for channel_id, search_title, rank in candidates:
        channel = channel_by_id.get(channel_id)
        if channel is None:
            continue
        title = str(channel.get("snippet", {}).get("title", search_title))
        description = str(channel.get("snippet", {}).get("description", ""))
        try:
            subscribers = int(channel.get("statistics", {}).get("subscriberCount", 0))
        except (TypeError, ValueError):
            subscribers = 0
        score = _channel_match_score(
            query,
            title,
            description,
            rank,
            subscribers,
        )
        if score is not None:
            ranked_channels.append((score, channel))
    if not ranked_channels:
        raise YouTubeResolverError(
            "No sufficiently relevant podcast channel was found; try the podcast title."
        )
    _score, selected_channel = max(ranked_channels, key=lambda candidate: candidate[0])
    try:
        uploads_playlist = selected_channel["contentDetails"]["relatedPlaylists"]["uploads"]
    except (KeyError, TypeError) as exc:
        raise YouTubeResolverError(
            "The selected YouTube channel has no uploads playlist."
        ) from exc

    return _resolve_uploads_playlist(
        api_key,
        str(uploads_playlist),
        min_duration_seconds=min_duration_seconds,
        get_json=get_json,
    )


def resolve_video_search(
    api_key: str,
    query: str,
    *,
    get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = youtube_api_get,
) -> MediaItem:
    """Resolve the best public YouTube video for a conversational play query."""

    if not api_key:
        raise YouTubeResolverError("YOUTUBE_API_KEY is not configured.")
    query = " ".join(query.split())
    if not query or len(query) > MAX_SEARCH_QUERY_CHARACTERS:
        raise YouTubeResolverError("YouTube search query has an invalid length.")
    payload = get_json(
        "search",
        {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": "relevance",
            "maxResults": 5,
            "safeSearch": "moderate",
            "videoEmbeddable": "true",
            "key": api_key,
        },
    )
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise YouTubeResolverError("YouTube returned an invalid video search response.")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), dict):
            continue
        video_id = str(item["id"].get("videoId", ""))
        snippet = item.get("snippet", {})
        if not VIDEO_ID_PATTERN.fullmatch(video_id) or not isinstance(snippet, dict):
            continue
        if snippet.get("liveBroadcastContent") in {"live", "upcoming"}:
            continue
        channel_id = str(snippet.get("channelId", ""))
        return MediaItem(
            video_id=video_id,
            title=str(snippet.get("title", "Untitled video")),
            published_at=str(snippet.get("publishedAt", "")),
            duration_seconds=0,
            channel_id=(channel_id if CHANNEL_ID_PATTERN.fullmatch(channel_id) else ""),
            channel_title=str(snippet.get("channelTitle", "")),
        )
    raise YouTubeResolverError(f"No playable YouTube video was found for {query!r}.")

"""Authenticated, allowlisted Windows endpoint for the room voice agent."""

from __future__ import annotations

import argparse
import json
import os
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from protocol import (
    MAX_BODY_BYTES,
    ActionRequest,
    ProtocolError,
    generate_key,
    read_key,
    verify_headers,
)
from youtube_resolver import (
    MediaItem,
    YouTubeResolverError,
    resolve_latest_episode,
    resolve_latest_search,
    resolve_video_search,
)
from windows_browser import WindowsBrowserController, WindowsBrowserError


ALLOWED_SITES = {
    "miton": "https://www.miton.cz/",
    "youtube": "https://www.youtube.com/",
}
MAX_WEB_QUERY_CHARACTERS = 120


class ActionError(ValueError):
    pass


def execute_action(
    request: ActionRequest,
    opener: Callable[..., bool] = webbrowser.open,
    latest_resolver: Callable[[], MediaItem] | None = None,
    search_resolver: Callable[[str], MediaItem] | None = None,
    video_resolver: Callable[[str], MediaItem] | None = None,
    browser: WindowsBrowserController | None = None,
) -> dict[str, str]:
    if request.target != "notebook":
        raise ActionError("This companion only accepts the notebook target.")

    if request.action == "web.open":
        if set(request.arguments) != {"site"}:
            raise ActionError("web.open requires exactly one site argument.")
        site = request.arguments.get("site")
        if site not in ALLOWED_SITES:
            raise ActionError("Requested site is not allowlisted.")
        url = ALLOWED_SITES[site]
        result = browser.open_site(site) if browser is not None else {"opened": url}
    elif request.action == "web.search":
        if set(request.arguments) != {"provider", "query"}:
            raise ActionError("web.search requires exactly provider and query.")
        provider = request.arguments.get("provider")
        query = request.arguments.get("query")
        if (
            provider != "google"
            or not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_WEB_QUERY_CHARACTERS
        ):
            raise ActionError("The requested web search is invalid.")
        query = " ".join(query.split())
        url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
        result = (
            browser.search_google(query) if browser is not None else {"opened": url}
        )
        result.update({"query": query, "provider": provider})
    elif request.action == "youtube.play_latest":
        expected = {"channel": "cinkatko", "selector": "latest_episode"}
        if request.arguments != expected:
            raise ActionError("The requested YouTube selection is not allowlisted.")
        if latest_resolver is None:
            raise ActionError("Latest Cinkatko playback is not configured.")
        try:
            media = latest_resolver()
        except YouTubeResolverError as exc:
            raise ActionError(f"Latest Cinkatko lookup failed: {exc}") from exc
        url = media.url
        result = {
            "title": media.title,
            "video_id": media.video_id,
            "source": media.source,
            "channel_title": media.channel_title,
        }
        if browser is not None:
            result.update(browser.play_youtube(media.video_id))
        else:
            result["opened"] = url
    elif request.action == "youtube.search_latest":
        if set(request.arguments) != {"query", "selector"}:
            raise ActionError("youtube.search_latest requires exactly query and selector.")
        query = request.arguments.get("query")
        if not isinstance(query, str) or request.arguments.get("selector") != "latest_episode":
            raise ActionError("The requested YouTube search is invalid.")
        if search_resolver is None:
            raise ActionError("YouTube title search is not configured.")
        try:
            media = search_resolver(query)
        except YouTubeResolverError as exc:
            raise ActionError(f"YouTube title search failed: {exc}") from exc
        url = media.url
        result = {
            "title": media.title,
            "video_id": media.video_id,
            "source": media.source,
            "channel_title": media.channel_title,
        }
        if browser is not None:
            result.update(browser.play_youtube(media.video_id))
        else:
            result["opened"] = url
    elif request.action == "youtube.search_play":
        if set(request.arguments) != {"query"}:
            raise ActionError("youtube.search_play requires exactly one query.")
        query = request.arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_WEB_QUERY_CHARACTERS:
            raise ActionError("The requested YouTube video search is invalid.")
        if video_resolver is None:
            raise ActionError("YouTube video search is not configured.")
        try:
            media = video_resolver(" ".join(query.split()))
        except YouTubeResolverError as exc:
            raise ActionError(f"YouTube video search failed: {exc}") from exc
        url = media.url
        result = {
            "title": media.title,
            "video_id": media.video_id,
            "source": media.source,
            "channel_title": media.channel_title,
            "query": " ".join(query.split()),
        }
        if browser is not None:
            result.update(browser.play_youtube(media.video_id))
        else:
            result["opened"] = url
    elif request.action == "web.open_first_result":
        if request.arguments or browser is None:
            raise ActionError("Opening the first result requires the dedicated browser.")
        return browser.open_first_google_result()
    elif request.action == "media.pause":
        if request.arguments or browser is None:
            raise ActionError("Pause requires the dedicated browser.")
        return browser.pause()
    elif request.action == "media.resume":
        if request.arguments or browser is None:
            raise ActionError("Continue requires the dedicated browser.")
        return browser.resume()
    elif request.action == "media.close":
        if request.arguments or browser is None:
            raise ActionError("Close requires the dedicated browser.")
        return browser.close()
    elif request.action == "tv.remote":
        if set(request.arguments) != {"command", "count"} or browser is None:
            raise ActionError("TV remote control requires command and count.")
        command = request.arguments.get("command")
        count = request.arguments.get("count")
        if (
            not isinstance(command, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= 20
        ):
            raise ActionError("The requested TV remote action is invalid.")
        repeated_methods = {
            "volume_up": "volume_up",
            "volume_down": "volume_down",
            "navigate_up": "navigate",
            "navigate_down": "navigate",
            "navigate_left": "navigate",
            "navigate_right": "navigate",
        }
        simple_methods = {
            "power_on": "power_on",
            "power_off": "power_off",
            "mute": "mute",
            "home": "home",
            "back": "back",
            "select": "select",
            "input": "input_source",
        }
        if command in repeated_methods:
            method = getattr(browser, repeated_methods[command], None)
            if method is None:
                raise ActionError("The selected TV bridge cannot perform this remote action.")
            if command.startswith("navigate_"):
                return method(command.removeprefix("navigate_"), count)
            return method(count)
        if command in simple_methods and count == 1:
            method = getattr(browser, simple_methods[command], None)
            if method is None:
                raise ActionError("The selected TV bridge cannot perform this remote action.")
            return method()
        raise ActionError("The requested TV remote command is not allowlisted.")
    else:
        raise ActionError("Action is not allowlisted by this companion.")

    if browser is None and not opener(url, new=2, autoraise=True):
        raise ActionError("Windows did not accept the browser-open request.")
    return result


def make_handler(
    secret: bytes,
    opener: Callable[..., bool] = webbrowser.open,
    latest_resolver: Callable[[], MediaItem] | None = None,
    search_resolver: Callable[[str], MediaItem] | None = None,
    video_resolver: Callable[[str], MediaItem] | None = None,
    browser: WindowsBrowserController | None = None,
):
    seen_nonces: dict[str, int] = {}
    nonce_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "RoomAgentCompanion/1"

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/health":
                self._json(404, {"status": "not_found"})
                return
            self._json(
                200,
                {
                    "status": "ok",
                    "device": "notebook",
                    "capabilities": [
                        "web.open",
                        "youtube.play_latest",
                        "youtube.search_latest",
                        "youtube.search_play",
                    ],
                    "youtube_api_configured": latest_resolver is not None,
                },
            )

        def do_POST(self) -> None:
            if self.path != "/v1/actions":
                self._json(404, {"status": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(400, {"status": "error", "error": "invalid_content_length"})
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._json(413, {"status": "error", "error": "invalid_body_size"})
                return
            body = self.rfile.read(length)
            try:
                with nonce_lock:
                    verify_headers(secret, body, self.headers, seen_nonces)
                request = ActionRequest.from_bytes(body)
                result = execute_action(
                    request,
                    opener=opener,
                    latest_resolver=latest_resolver,
                    search_resolver=search_resolver,
                    video_resolver=video_resolver,
                    browser=browser,
                )
            except (ProtocolError, ActionError, WindowsBrowserError) as exc:
                self._json(403, {"status": "rejected", "error": str(exc)})
                return
            self._json(
                200,
                {
                    "status": "executed",
                    "request_id": request.request_id,
                    "action": request.action,
                    **result,
                },
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.client_address[0]} - {fmt % args}")

    return Handler


def load_environment_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip('"').strip("'")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--key-file", type=Path, default=Path(__file__).with_name("pairing.key"))
    parser.add_argument(
        "--generate-key",
        action="store_true",
        help="Create a new pairing key at --key-file and exit.",
    )
    parser.add_argument(
        "--check-youtube",
        action="store_true",
        help="Resolve and print the latest Cinkatko episode without opening a browser.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    load_environment_file(Path(__file__).with_name(".env"))

    youtube_api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    latest_resolver = None
    search_resolver = None
    video_resolver = None
    if youtube_api_key:
        handle = os.getenv("CINKATKO_HANDLE", "@mitoncz").strip()
        try:
            min_duration = int(os.getenv("MIN_EPISODE_SECONDS", "600"))
        except ValueError:
            print("ERROR: MIN_EPISODE_SECONDS must be an integer.")
            return 2
        if min_duration < 1:
            print("ERROR: MIN_EPISODE_SECONDS must be positive.")
            return 2
        latest_resolver = lambda: resolve_latest_episode(
            youtube_api_key,
            handle=handle,
            min_duration_seconds=min_duration,
        )
        search_resolver = lambda query: resolve_latest_search(
            youtube_api_key,
            query,
            min_duration_seconds=min_duration,
        )
        video_resolver = lambda query: resolve_video_search(youtube_api_key, query)

    if args.check_youtube:
        if latest_resolver is None:
            print("ERROR: YOUTUBE_API_KEY is not configured in room_agent/.env.")
            return 2
        try:
            media = latest_resolver()
        except YouTubeResolverError as exc:
            print(f"ERROR: {exc}")
            return 2
        print(f"Selected: {media.title}")
        if media.channel_title:
            print(f"Channel: {media.channel_title}")
        print(f"Published: {media.published_at}")
        print(f"Duration: {media.duration_seconds} seconds")
        print(f"URL: {media.url}")
        return 0

    try:
        if args.generate_key:
            generate_key(args.key_file)
            print(f"Created pairing key: {args.key_file}")
            return 0
        secret = read_key(args.key_file)
    except ProtocolError as exc:
        print(f"ERROR: {exc}")
        return 2

    server = ThreadingHTTPServer(
        (args.bind, args.port),
        make_handler(
            secret,
            latest_resolver=latest_resolver,
            search_resolver=search_resolver,
            video_resolver=video_resolver,
        ),
    )
    print(f"Notebook companion ready on http://{args.bind}:{args.port}")
    print(
        "Capabilities: web.open, youtube.play_latest, "
        "youtube.search_latest, youtube.search_play"
    )
    print(f"YouTube API configured: {'yes' if latest_resolver else 'no'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping companion.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

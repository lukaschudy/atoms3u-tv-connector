"""Production iPhone Action Button host for a Windows room computer."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

from iphone_gateway import (
    IphoneCommandServer,
    IphoneDeviceRegistry,
    IphoneGatewayError,
    default_iphone_audit_path,
    default_iphone_registry_path,
)
from intent_router import NoActivation, NoRoute
from local_llm import FallbackLlmRouter, LlmRouter, LocalLlmRouter, hybrid_route
from openrouter_llm import DEFAULT_OPENROUTER_MODEL, OpenRouterLlmRouter
from protocol import ProtocolError, read_key
from tv_connector import (
    BleTvConnectorClient,
    TvConnectorClient,
    TvConnectorError,
    UsbTvConnectorClient,
)
from windows_browser import WindowsBrowserController, WindowsBrowserError
from windows_companion import ActionError, execute_action, load_environment_file
from youtube_resolver import (
    YouTubeResolverError,
    resolve_latest_episode,
    resolve_latest_search,
    resolve_video_search,
)


class WindowsHostError(RuntimeError):
    pass


def build_router(
    *,
    openrouter_api_key: str,
    openrouter_model: str,
    openrouter_timeout: float,
    local_url: str | None,
    local_timeout: float,
) -> LlmRouter | None:
    routers: list[LlmRouter] = []
    if openrouter_api_key.strip():
        routers.append(
            OpenRouterLlmRouter(
                openrouter_api_key,
                model=openrouter_model,
                timeout=openrouter_timeout,
            )
        )
    if local_url:
        routers.append(LocalLlmRouter(local_url, timeout=local_timeout))
    if len(routers) > 1:
        return FallbackLlmRouter(tuple(routers))
    return routers[0] if routers else None


class WindowsIphoneSession:
    def __init__(
        self,
        browser: WindowsBrowserController,
        *,
        latest_resolver=None,
        search_resolver=None,
        video_resolver=None,
        llm_router: LlmRouter | None = None,
        tv_connector: TvConnectorClient | None = None,
        treat_tv_as_local_display: bool = False,
        log_transcripts: bool = False,
    ) -> None:
        self.browser = browser
        self.latest_resolver = latest_resolver
        self.search_resolver = search_resolver
        self.video_resolver = video_resolver
        self.llm_router = llm_router
        self.tv_connector = tv_connector
        self.treat_tv_as_local_display = treat_tv_as_local_display
        self.log_transcripts = log_transcripts
        self.active_target: str | None = None
        self.active_surface: str | None = None
        self.active_surface_target: str | None = None
        self._lock = threading.Lock()

    def execute(self, transcript: str) -> dict[str, object]:
        with self._lock:
            print(
                "IPHONE HEARD: "
                + (transcript if self.log_transcripts else "[redacted]")
            )
            if self.llm_router is not None:
                self.llm_router.last_backend = None
            try:
                action = hybrid_route(
                    transcript,
                    active_target=self.active_surface_target or self.active_target,
                    active_surface=self.active_surface,
                    require_activation=False,
                    default_open_target="tv" if self.tv_connector is not None else "notebook",
                    llm_router=self.llm_router,
                )
            except (NoActivation, NoRoute) as exc:
                raise WindowsHostError(str(exc)) from exc
            semantic_backend = (
                self.llm_router.last_backend if self.llm_router is not None else None
            )
            requested_target = action.target
            executor = self.browser
            executed_target = action.target
            if action.target == "tv":
                if self.tv_connector is not None:
                    executor = self.tv_connector
                    executed_target = "tv-connector"
                    action = replace(action, target="notebook")
                elif self.treat_tv_as_local_display:
                    executed_target = "notebook"
                    action = replace(action, target="notebook")
                else:
                    raise WindowsHostError(
                        "This Windows host has no TV bridge. Say laptop/computer, "
                        "configure the Wi-Fi ATOMS3U connector with "
                        "setup_tv_connector.py connect-host, or enable "
                        "--treat-tv-as-local-display for an attached display."
                    )
            try:
                if action.action == "app.open":
                    app = action.arguments.get("app")
                    if (
                        executor is not self.tv_connector
                        or set(action.arguments) != {"app"}
                        or app not in {"netflix", "youtube"}
                    ):
                        raise ActionError("The requested TV app is not allowlisted.")
                    response = self.tv_connector.launch_app(app)
                else:
                    response = execute_action(
                        action,
                        latest_resolver=self.latest_resolver,
                        search_resolver=self.search_resolver,
                        video_resolver=self.video_resolver,
                        browser=executor,
                    )
            except (
                ActionError,
                TvConnectorError,
                WindowsBrowserError,
                YouTubeResolverError,
            ) as exc:
                raise WindowsHostError(str(exc)) from exc

            content_target = "tv" if executed_target == "tv-connector" else "notebook"
            if action.action == "media.close":
                self.active_target = None
                self.active_surface = None
                self.active_surface_target = None
            elif action.action == "tv.remote":
                if self.active_surface_target is None:
                    self.active_target = "tv"
            elif action.action not in {"tv.remote", "media.pause", "media.resume"}:
                self.active_target = content_target
                self.active_surface_target = (
                    requested_target if requested_target in {"tv", "notebook"} else content_target
                )
                if action.action in {
                    "youtube.play_latest",
                    "youtube.search_latest",
                    "youtube.search_play",
                } or (
                    action.action == "web.open"
                    and action.arguments.get("site") == "youtube"
                ):
                    self.active_surface = "youtube"
                elif action.action == "web.open":
                    self.active_surface = str(action.arguments.get("site", "web"))
                elif action.action == "web.search":
                    self.active_surface = "google"
                elif action.action == "app.open":
                    self.active_surface = str(action.arguments.get("app", "app"))
                else:
                    self.active_surface = None
            result: dict[str, object] = {
                "active_target": self.active_target or "none",
                "action": action.action,
                "target": requested_target,
                "executed_target": executed_target,
            }
            if semantic_backend:
                result["semantic_backend"] = semantic_backend
            for key in (
                "title",
                "channel_title",
                "opened",
                "media_status",
                "player",
                "connector_profile",
                "connector_purpose",
                "connector_transport",
            ):
                if response.get(key) is not None:
                    result[key] = response[key]
            print(f"WINDOWS ACTION: {action.action} -> {executed_target}")
            return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument(
        "--devices-file",
        type=Path,
        default=default_iphone_registry_path(),
    )
    parser.add_argument(
        "--audit-file",
        type=Path,
        default=default_iphone_audit_path(),
    )
    parser.add_argument("--browser", type=Path)
    parser.add_argument(
        "--browser-profile",
        type=Path,
        help="Dedicated Room Agent browser profile directory.",
    )
    parser.add_argument(
        "--personal-browser-profile",
        action="store_true",
        help="Open a tracked window in the normal signed-in browser profile.",
    )
    parser.add_argument(
        "--tv-connector-url",
        default=os.getenv("TV_CONNECTOR_URL", "").strip() or None,
        help="HTTP(S) origin of an ATOMS3U connector or its simulator.",
    )
    parser.add_argument(
        "--tv-connector-ble-name",
        default=os.getenv("TV_CONNECTOR_BLE_NAME", "").strip() or None,
        help="Bluetooth name of an ATOMS3U connector (preferred over Wi-Fi).",
    )
    parser.add_argument(
        "--tv-connector-key-file",
        type=Path,
        default=(
            Path(os.environ["TV_CONNECTOR_KEY_FILE"])
            if os.getenv("TV_CONNECTOR_KEY_FILE", "").strip()
            else None
        ),
    )
    parser.add_argument(
        "--tv-connector-profile",
        default=os.getenv("TV_CONNECTOR_PROFILE", "generic-usb-hid-v1"),
    )
    parser.add_argument(
        "--tv-connector-timeout",
        type=float,
        default=os.getenv("TV_CONNECTOR_TIMEOUT_SECONDS", "8.0"),
    )
    parser.add_argument(
        "--treat-tv-as-local-display",
        action="store_true",
        help="Route explicit TV/big-screen commands to this Windows display.",
    )
    parser.add_argument("--log-transcripts", action="store_true")
    parser.add_argument(
        "--openrouter-model",
        default=os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
    )
    parser.add_argument(
        "--openrouter-timeout",
        type=float,
        default=os.getenv("OPENROUTER_TIMEOUT_SECONDS", "6.0"),
    )
    parser.add_argument("--llm-url", default=os.getenv("LOCAL_LLM_URL"))
    parser.add_argument("--llm-timeout", type=float, default=8.0)
    parser.add_argument(
        "--text",
        help="Execute one typed transcript and exit instead of starting HTTP.",
    )
    return parser


def main() -> int:
    load_environment_file(Path(__file__).with_name(".env"))
    args = build_parser().parse_args()
    if not 1_024 <= args.port <= 65_535:
        print("ERROR: --port must be between 1024 and 65535.", file=sys.stderr)
        return 2
    if not 0.5 <= args.openrouter_timeout <= 30:
        print("ERROR: --openrouter-timeout must be between 0.5 and 30.", file=sys.stderr)
        return 2
    if not 0.5 <= args.llm_timeout <= 30:
        print("ERROR: --llm-timeout must be between 0.5 and 30.", file=sys.stderr)
        return 2
    if not 0.5 <= args.tv_connector_timeout <= 30:
        print("ERROR: --tv-connector-timeout must be between 0.5 and 30.", file=sys.stderr)
        return 2

    youtube_api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    latest_resolver = None
    search_resolver = None
    video_resolver = None
    if youtube_api_key:
        handle = os.getenv("CINKATKO_HANDLE", "@mitoncz").strip()
        try:
            min_duration = int(os.getenv("MIN_EPISODE_SECONDS", "600"))
        except ValueError:
            print("ERROR: MIN_EPISODE_SECONDS must be an integer.", file=sys.stderr)
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

    try:
        browser = WindowsBrowserController(
            browser=args.browser,
            profile_dir=args.browser_profile,
            personal_profile=args.personal_browser_profile,
        )
        router = build_router(
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_model=args.openrouter_model,
            openrouter_timeout=args.openrouter_timeout,
            local_url=args.llm_url,
            local_timeout=args.llm_timeout,
        )
        tv_connector = None
        if args.tv_connector_url and args.tv_connector_ble_name:
            raise WindowsHostError(
                "Configure either TV_CONNECTOR_BLE_NAME or TV_CONNECTOR_URL, not both."
            )
        if args.tv_connector_url or args.tv_connector_ble_name:
            if args.tv_connector_key_file is None:
                raise WindowsHostError(
                    "TV_CONNECTOR_KEY_FILE is required when a TV connector is configured."
                )
            connector_key = read_key(args.tv_connector_key_file)
            if args.tv_connector_ble_name:
                tv_connector = BleTvConnectorClient(
                    args.tv_connector_ble_name,
                    connector_key,
                    profile=args.tv_connector_profile,
                    timeout=args.tv_connector_timeout,
                )
            else:
                tv_connector = UsbTvConnectorClient(
                    args.tv_connector_url,
                    connector_key,
                    profile=args.tv_connector_profile,
                    timeout=args.tv_connector_timeout,
                )
        session = WindowsIphoneSession(
            browser,
            latest_resolver=latest_resolver,
            search_resolver=search_resolver,
            video_resolver=video_resolver,
            llm_router=router,
            tv_connector=tv_connector,
            treat_tv_as_local_display=args.treat_tv_as_local_display,
            log_transcripts=args.log_transcripts,
        )
        if args.text:
            print(session.execute(args.text))
            return 0

        registry = IphoneDeviceRegistry(args.devices_file)
        server = IphoneCommandServer(
            registry.authenticate,
            session.execute,
            pairing_redeemer=registry.redeem_pairing,
            host=args.bind,
            port=args.port,
            audit_path=args.audit_file,
        )
    except (
        IphoneGatewayError,
        OSError,
        ProtocolError,
        TvConnectorError,
        ValueError,
        WindowsBrowserError,
        WindowsHostError,
    ) as exc:
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) == 98:
            print(
                f"ERROR: Room Agent is already running on http://{args.bind}:{args.port}.",
                file=sys.stderr,
            )
            return 2
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Windows iPhone host ready on http://{args.bind}:{server.port}")
    print("Transport: loopback only; publish privately with Tailscale Serve")
    print(f"Semantic router: {'configured' if router else 'deterministic only'}")
    print(f"YouTube lookup: {'configured' if search_resolver else 'not configured'}")
    print(f"TV connector: {'configured' if tv_connector else 'not configured'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping Windows iPhone host.")
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

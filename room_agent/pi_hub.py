"""Raspberry Pi voice hub for the first live two-device checkpoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_state import AgentState, StateEvent, StateMachine
from display_manager import (
    DisplayConfigError,
    DisplayControlError,
    load_display_manager,
    status_document,
)
from hermes_llm import DEFAULT_HERMES_MODEL, HermesLlmRouter
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
from pi_player import ChromiumHdmiPlayer, PiPlayerError
from protocol import ActionRequest, ProtocolError, read_key, signed_headers
from respeaker_led import LedUnavailable, ReSpeakerLedObserver
from room_canvas import RoomCanvasServer, RoomCanvasState
from youtube_resolver import (
    MediaItem,
    YouTubeResolverError,
    resolve_latest_episode,
    resolve_latest_search,
    resolve_video_search,
)


class HubError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandOutcome:
    active_target: str | None
    action: str
    target: str
    response: dict
    active_surface: str | None = None
    active_surface_target: str | None = None


def run_checked(command: list[str], description: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise HubError(f"{description} executable was not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown error").strip()
        raise HubError(f"{description} failed: {detail}") from exc


def capture(audio_device: str, seconds: int, raw_path: Path, mono_path: Path) -> None:
    run_checked(
        [
            "arecord",
            "-D",
            audio_device,
            "-f",
            "S16_LE",
            "-r",
            "16000",
            "-c",
            "6",
            "-d",
            str(seconds),
            str(raw_path),
        ],
        "Audio capture",
    )
    run_checked(["sox", str(raw_path), str(mono_path), "remix", "1"], "Channel extraction")


def hands_free_commands(
    audio_device: str,
    mono_path: Path,
    *,
    max_seconds: int,
    silence_seconds: float,
    threshold_percent: float,
) -> tuple[list[str], list[str]]:
    recorder = [
        "arecord",
        "-q",
        "-D",
        audio_device,
        "-f",
        "S16_LE",
        "-r",
        "16000",
        "-c",
        "6",
        "-t",
        "raw",
    ]
    threshold = f"{threshold_percent:g}%"
    processor = [
        "sox",
        "-q",
        "-t",
        "raw",
        "-r",
        "16000",
        "-e",
        "signed-integer",
        "-b",
        "16",
        "-c",
        "6",
        "-",
        str(mono_path),
        "remix",
        "1",
        "silence",
        "1",
        "0.2",
        threshold,
        "1",
        f"{silence_seconds:g}",
        threshold,
        "trim",
        "0",
        str(max_seconds),
    ]
    return recorder, processor


def capture_until_silence(
    audio_device: str,
    mono_path: Path,
    *,
    max_seconds: int,
    silence_seconds: float,
    threshold_percent: float,
) -> None:
    recorder_command, processor_command = hands_free_commands(
        audio_device,
        mono_path,
        max_seconds=max_seconds,
        silence_seconds=silence_seconds,
        threshold_percent=threshold_percent,
    )
    try:
        recorder = subprocess.Popen(
            recorder_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HubError("Hands-free recorder executable was not found: arecord") from exc

    processor = None
    try:
        if recorder.stdout is None:
            raise HubError("Hands-free recorder did not expose an audio stream.")
        processor = subprocess.Popen(
            processor_command,
            stdin=recorder.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        recorder.stdout.close()
        _output, processor_error = processor.communicate()
        if processor.returncode != 0:
            detail = (processor_error or b"unknown SoX error").decode(
                "utf-8", errors="replace"
            )
            raise HubError(f"Hands-free speech capture failed: {detail.strip()}")
    except FileNotFoundError as exc:
        raise HubError("Hands-free audio processor executable was not found: sox") from exc
    finally:
        if recorder.poll() is None:
            recorder.terminate()
            try:
                recorder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                recorder.kill()
                recorder.wait(timeout=2)
        if recorder.stderr is not None:
            recorder.stderr.close()
        if processor is not None and processor.stderr is not None:
            processor.stderr.close()

    if not mono_path.is_file() or mono_path.stat().st_size <= 44:
        raise HubError("Hands-free speech capture produced no usable audio.")


def transcribe(
    whisper_cli: Path,
    model: Path,
    audio_path: Path,
    language: str,
) -> str:
    if not whisper_cli.is_file():
        raise HubError(f"whisper-cli does not exist: {whisper_cli}")
    if not model.is_file():
        raise HubError(f"Whisper model does not exist: {model}")
    prompt = (
        "Cinkatko, Miton. Play the latest Cinkatko podcast on the TV. "
        "What is the weight of an elephant? Room Agent, pause. "
        "Room Agent, continue. Quit. Close tab."
        if language == "en"
        else "Cinkátko, Miton. Pusť poslední díl Cinkátka na televizi. "
        "Room Agent, pozastav. Room Agent, pokračuj."
    )
    result = run_checked(
        [
            str(whisper_cli),
            "-m",
            str(model),
            "-f",
            str(audio_path),
            "-l",
            language,
            "-nt",
            "--prompt",
            prompt,
        ],
        "Speech recognition",
    )
    transcript = " ".join(line.strip() for line in result.stdout.splitlines() if line.strip())
    if not transcript:
        raise HubError("Speech recognition returned an empty transcript.")
    return transcript


def dispatch(pc_url: str, secret: bytes, body: bytes) -> dict:
    endpoint = pc_url.rstrip("/") + "/v1/actions"
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=signed_headers(secret, body),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HubError(f"Notebook rejected the action ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HubError(f"Notebook companion is unreachable: {exc}") from exc


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


def execute_local_tv_action(
    action: ActionRequest,
    *,
    latest_resolver,
    player: ChromiumHdmiPlayer,
    search_resolver=None,
    video_resolver=None,
) -> dict[str, str]:
    if action.target != "tv":
        raise HubError("The display executor only accepts the TV target.")
    if action.action == "app.open":
        if set(action.arguments) != {"app"}:
            raise HubError("The requested native TV app action is invalid.")
        app = action.arguments.get("app")
        if not isinstance(app, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,47}", app
        ):
            raise HubError("The requested native TV app name is invalid.")
        launch_app = getattr(player, "launch_app", None)
        if not callable(launch_app):
            raise HubError("Native TV app control is not configured.")
        try:
            playback = launch_app(app)
        except (PiPlayerError, DisplayControlError) as exc:
            raise HubError(f"Native TV app launch failed: {exc}") from exc
        return {
            "status": "executed",
            "request_id": action.request_id,
            "action": action.action,
            **playback,
        }
    if action.action == "web.open":
        if set(action.arguments) != {"site"}:
            raise HubError("The requested TV site action is invalid.")
        site = action.arguments.get("site")
        if site not in {"miton", "youtube"}:
            raise HubError("The requested TV site is not allowlisted.")
        try:
            playback = player.open_site(site)
        except (PiPlayerError, DisplayControlError) as exc:
            raise HubError(f"TV website launch failed: {exc}") from exc
        return {
            "status": "executed",
            "request_id": action.request_id,
            "action": action.action,
            **playback,
        }
    if action.action == "web.search":
        if set(action.arguments) != {"provider", "query"}:
            raise HubError("The requested TV search action is invalid.")
        provider = action.arguments.get("provider")
        query = action.arguments.get("query")
        if provider != "google" or not isinstance(query, str):
            raise HubError("The requested TV search is not allowlisted.")
        try:
            playback = player.search_google(query)
        except (PiPlayerError, DisplayControlError) as exc:
            raise HubError(f"TV Google search failed: {exc}") from exc
        return {
            "status": "executed",
            "request_id": action.request_id,
            "action": action.action,
            "query": query,
            "provider": provider,
            **playback,
        }
    if action.action == "web.open_first_result" and action.arguments == {}:
        try:
            playback = player.open_first_google_result()
        except (PiPlayerError, DisplayControlError) as exc:
            raise HubError(f"TV first Google result failed: {exc}") from exc
        return {
            "status": "executed",
            "request_id": action.request_id,
            "action": action.action,
            **playback,
        }
    if action.action == "media.pause" and action.arguments == {}:
        try:
            playback = player.pause()
        except (PiPlayerError, DisplayControlError) as exc:
            raise HubError(f"TV pause failed: {exc}") from exc
        return {
            "status": "executed",
            "request_id": action.request_id,
            "action": action.action,
            **playback,
        }
    if action.action == "media.resume" and action.arguments == {}:
        try:
            playback = player.resume()
        except (PiPlayerError, DisplayControlError) as exc:
            raise HubError(f"TV continue failed: {exc}") from exc
        return {
            "status": "executed",
            "request_id": action.request_id,
            "action": action.action,
            **playback,
        }
    if action.action == "media.close" and action.arguments == {}:
        try:
            playback = player.close()
            if getattr(player, "home_url", None) is not None:
                home = player.show_home()
                playback["home"] = home["opened"]
        except (PiPlayerError, DisplayControlError) as exc:
            raise HubError(f"TV close failed: {exc}") from exc
        return {
            "status": "executed",
            "request_id": action.request_id,
            "action": action.action,
            **playback,
        }
    if action.action == "youtube.search_latest":
        if set(action.arguments) != {"query", "selector"}:
            raise HubError("The requested YouTube title search is invalid.")
        query = action.arguments.get("query")
        if not isinstance(query, str) or action.arguments.get("selector") != "latest_episode":
            raise HubError("The requested YouTube title search is invalid.")
        if search_resolver is None:
            raise HubError("YouTube title search is not configured on the Raspberry Pi.")
        try:
            media = search_resolver(query)
            playback = player.play_youtube(media.video_id)
        except (YouTubeResolverError, PiPlayerError, DisplayControlError) as exc:
            raise HubError(f"TV YouTube search failed: {exc}") from exc
        return {
            "status": "executed",
            "request_id": action.request_id,
            "action": action.action,
            "title": media.title,
            "video_id": media.video_id,
            "channel_title": media.channel_title,
            **playback,
        }
    if action.action == "youtube.search_play":
        if set(action.arguments) != {"query"}:
            raise HubError("The requested YouTube video search is invalid.")
        query = action.arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise HubError("The requested YouTube video search is invalid.")
        if video_resolver is None:
            raise HubError("YouTube video search is not configured on the Raspberry Pi.")
        try:
            media = video_resolver(query)
            playback = player.play_youtube(media.video_id)
        except (YouTubeResolverError, PiPlayerError, DisplayControlError) as exc:
            raise HubError(f"TV YouTube video search failed: {exc}") from exc
        return {
            "status": "executed",
            "request_id": action.request_id,
            "action": action.action,
            "title": media.title,
            "video_id": media.video_id,
            "channel_title": media.channel_title,
            "query": " ".join(query.split()),
            **playback,
        }
    expected = {"channel": "cinkatko", "selector": "latest_episode"}
    if action.action != "youtube.play_latest" or action.arguments != expected:
        raise HubError("The requested local TV action is not allowlisted.")
    if latest_resolver is None:
        raise HubError("YouTube lookup is not configured on the Raspberry Pi.")

    try:
        media: MediaItem = latest_resolver()
        playback = player.play_youtube(media.video_id)
    except (YouTubeResolverError, PiPlayerError, DisplayControlError) as exc:
        raise HubError(f"TV playback failed: {exc}") from exc

    return {
        "status": "executed",
        "request_id": action.request_id,
        "action": action.action,
        "title": media.title,
        "video_id": media.video_id,
        "channel_title": media.channel_title,
        **playback,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-url", help="Optional notebook companion base URL.")
    parser.add_argument("--key-file", type=Path, default=Path(__file__).with_name("pairing.key"))
    parser.add_argument(
        "--whisper-cli",
        type=Path,
        default=Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=Path.home() / "whisper.cpp" / "models" / "ggml-base.bin",
    )
    parser.add_argument("--language", choices=("en", "cs"), default="en")
    parser.add_argument("--seconds", type=int, default=7)
    parser.add_argument("--audio-device", default="plughw:CARD=ArrayUAC10,DEV=0")
    text_mode = parser.add_mutually_exclusive_group()
    text_mode.add_argument(
        "--text",
        help="Skip audio/STT and execute one supplied command.",
    )
    text_mode.add_argument(
        "--text-console",
        action="store_true",
        help=(
            "Open a persistent typed-command console that preserves the active "
            "browser/media session between commands."
        ),
    )
    parser.add_argument(
        "--button-mode",
        action="store_true",
        help=(
            "Wait for an Enter-key USB button before each bounded command and "
            "return to READY afterward."
        ),
    )
    parser.add_argument(
        "--hands-free",
        action="store_true",
        help=(
            "Wait automatically for one utterance, execute it, then disarm after "
            "success. Intended for supervised boot startup."
        ),
    )
    parser.add_argument(
        "--iphone-gateway",
        action="store_true",
        help=(
            "Accept authenticated transcripts from an iPhone Action Button "
            "Shortcut; this can run together with --hands-free."
        ),
    )
    parser.add_argument(
        "--iphone-bind",
        default="127.0.0.1",
        help="Address for the iPhone gateway (default: loopback for Tailscale Serve).",
    )
    parser.add_argument(
        "--iphone-port",
        type=int,
        default=8770,
        help="Local iPhone transcript gateway port (default: 8770).",
    )
    parser.add_argument(
        "--iphone-devices-file",
        type=Path,
        default=default_iphone_registry_path(),
        help="Hashed iPhone enrollment registry created by configure_iphone.py.",
    )
    parser.add_argument(
        "--iphone-audit-file",
        type=Path,
        default=default_iphone_audit_path(),
        help="Private metadata-only iPhone request audit log.",
    )
    parser.add_argument(
        "--log-transcripts",
        action="store_true",
        help="Print recognized command text to the journal (disabled by default).",
    )
    parser.add_argument("--speech-threshold-percent", type=float, default=2.0)
    parser.add_argument("--silence-seconds", type=float, default=1.2)
    parser.add_argument("--max-utterance-seconds", type=int, default=12)
    parser.add_argument(
        "--no-led",
        action="store_true",
        help="Disable automatic ReSpeaker state LED control.",
    )
    parser.add_argument("--led-brightness", type=int, default=10)
    parser.add_argument(
        "--chromium",
        help="Chromium executable for local HDMI playback (auto-detected by default).",
    )
    parser.add_argument(
        "--chromium-profile",
        type=Path,
        default=Path.home() / ".config" / "room-agent" / "chromium",
    )
    parser.add_argument(
        "--display-config",
        type=Path,
        default=Path(__file__).with_name("display_endpoints.json"),
        help=(
            "Trusted native display enrollment file. A missing file keeps the "
            "existing HDMI-only behavior."
        ),
    )
    parser.add_argument(
        "--display-status",
        action="store_true",
        help="Probe enrolled native displays and HDMI, print JSON, then exit.",
    )
    parser.add_argument(
        "--room-canvas",
        action="store_true",
        help="Show the local Room Canvas on HDMI and return to it after closing media.",
    )
    parser.add_argument(
        "--canvas-port",
        type=int,
        default=8768,
        help="Loopback HTTP port used by Room Canvas (default: 8768).",
    )
    parser.add_argument(
        "--hermes-url",
        default=os.getenv("HERMES_API_URL"),
        help="Optional authenticated loopback Hermes Agent API URL.",
    )
    parser.add_argument(
        "--hermes-model",
        default=os.getenv("HERMES_API_MODEL", DEFAULT_HERMES_MODEL),
        help="Model name advertised by the local Hermes API server.",
    )
    parser.add_argument(
        "--hermes-timeout",
        type=float,
        default=os.getenv("HERMES_TIMEOUT_SECONDS", "25.0"),
        help="Maximum seconds for a bounded Hermes tool-planning turn.",
    )
    parser.add_argument(
        "--llm-url",
        default=os.getenv("LOCAL_LLM_URL"),
        help="Optional loopback llama.cpp server URL for semantic routing fallback.",
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=8.0,
        help="Maximum seconds to wait for the local semantic router.",
    )
    parser.add_argument(
        "--openrouter-model",
        default=os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        help="OpenRouter model slug used when OPENROUTER_API_KEY is configured.",
    )
    parser.add_argument(
        "--openrouter-timeout",
        type=float,
        default=os.getenv("OPENROUTER_TIMEOUT_SECONDS", "4.0"),
        help="Maximum seconds to wait before falling back from OpenRouter.",
    )
    return parser


def print_state(event: StateEvent) -> None:
    suffix = f" | {event.detail}" if event.detail else ""
    print(f"STATE: {event.state.value}{suffix}")


def print_observer_error(observer, exc: Exception) -> None:
    name = getattr(observer, "__name__", observer.__class__.__name__)
    print(f"WARNING: state observer {name} failed: {exc}", file=sys.stderr)


def build_semantic_router(
    *,
    hermes_url: str | None = None,
    hermes_api_key: str = "",
    hermes_model: str = DEFAULT_HERMES_MODEL,
    hermes_timeout: float = 25.0,
    openrouter_api_key: str = "",
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL,
    openrouter_timeout: float = 4.0,
    local_url: str | None = None,
    local_timeout: float = 8.0,
) -> LlmRouter | None:
    """Prefer bounded Hermes, then direct OpenRouter, then local Qwen."""

    routers: list[LlmRouter] = []
    if bool(hermes_url) != bool(hermes_api_key.strip()):
        raise ValueError("Hermes URL and API key must be configured together.")
    if hermes_url:
        routers.append(
            HermesLlmRouter(
                hermes_url,
                hermes_api_key,
                model=hermes_model,
                timeout=hermes_timeout,
            )
        )
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


def discard_pending_button_presses() -> None:
    """Prevent button presses made during processing from arming another capture."""
    if not sys.stdin.isatty():
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (ImportError, OSError, ValueError):
        pass


def wait_for_listen_button(*, button_mode: bool) -> None:
    discard_pending_button_presses()
    if button_mode:
        prompt = (
            "Press the listen button (or Enter), then say: "
            "Play the latest Cinkatko podcast on the TV... "
        )
    else:
        prompt = (
            "Press Enter, then say: "
            "Play the latest Cinkatko podcast on the TV... "
        )
    input(prompt)


def prompt_text_console(
    active_target: str | None,
    *,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> str | None:
    """Read one executable command while handling console-only directives."""

    while True:
        try:
            command = input_fn("room-agent> ").strip()
        except EOFError:
            return None
        if not command:
            continue
        directive = command.casefold()
        if directive in {":exit", ":quit"}:
            return None
        if directive == ":status":
            output(f"Active target: {active_target or 'none'}")
            continue
        if directive == ":help":
            output(
                "Type a normal room command. Console controls: "
                ":status, :help, :exit."
            )
            continue
        return command


def capture_transcript(
    args: argparse.Namespace,
    state: StateMachine | None,
) -> str:
    """Capture one ReSpeaker utterance, optionally publishing input-only states."""

    with tempfile.TemporaryDirectory(prefix="room-agent-") as temp_dir:
        raw_path = Path(temp_dir) / "command-6ch.wav"
        mono_path = Path(temp_dir) / "command.wav"
        if args.hands_free:
            if state is not None:
                state.transition(AgentState.LISTENING, "waiting for one utterance")
            capture_until_silence(
                args.audio_device,
                mono_path,
                max_seconds=args.max_utterance_seconds,
                silence_seconds=args.silence_seconds,
                threshold_percent=args.speech_threshold_percent,
            )
        else:
            wait_for_listen_button(button_mode=args.button_mode)
            if state is not None:
                state.transition(AgentState.LISTENING, f"{args.seconds} seconds")
            capture(args.audio_device, args.seconds, raw_path, mono_path)
        if state is not None:
            state.transition(AgentState.TRANSCRIBING, f"language={args.language}")
        return transcribe(
            args.whisper_cli.expanduser(),
            args.model.expanduser(),
            mono_path,
            args.language,
        )


def run_attempt(
    args: argparse.Namespace,
    state: StateMachine,
    secret: bytes | None,
    latest_resolver,
    search_resolver,
    player: ChromiumHdmiPlayer,
    active_target: str | None,
    llm_router: LlmRouter | None,
    *,
    active_surface: str | None = None,
    active_surface_target: str | None = None,
    video_resolver=None,
    transcript_override: str | None = None,
    canvas_state: RoomCanvasState | None = None,
    require_activation_override: bool | None = None,
) -> CommandOutcome:
    if canvas_state is not None:
        canvas_state.start_command()
    if transcript_override is not None:
        transcript = transcript_override
    elif args.text:
        transcript = args.text
    else:
        transcript = capture_transcript(args, state)

    print(f"HEARD: {transcript if args.log_transcripts else '[redacted]'}")
    if canvas_state is not None:
        canvas_state.record_transcript(transcript)
    state.transition(AgentState.ROUTING)
    if llm_router is not None:
        llm_router.last_backend = None
    require_activation = (
        require_activation_override
        if require_activation_override is not None
        else args.hands_free and active_target is not None
    )
    action = hybrid_route(
        transcript,
        active_target=active_surface_target or active_target,
        active_surface=active_surface,
        require_activation=require_activation,
        default_open_target="notebook" if args.pc_url else "tv",
        llm_router=llm_router,
    )
    print(f"ROUTING: {action.action} -> {action.target}")
    if llm_router is not None and llm_router.last_backend is not None:
        print(f"SEMANTIC: {llm_router.last_backend}")
    if canvas_state is not None:
        canvas_state.record_action(action)
    state.transition(AgentState.EXECUTING, f"{action.action} -> {action.target}")
    if action.target == "tv":
        response = execute_local_tv_action(
            action,
            latest_resolver=latest_resolver,
            player=player,
            search_resolver=search_resolver,
            video_resolver=video_resolver,
        )
    else:
        if not args.pc_url or secret is None:
            raise HubError("Notebook control requires --pc-url and a pairing key.")
        response = dispatch(args.pc_url, secret, action.to_bytes())
    if canvas_state is not None:
        canvas_state.record_result(response)
    state.transition(AgentState.SUCCESS, response.get("status", "executed"))
    if response.get("title"):
        print(f"SELECTED: {response['title']}")
    if response.get("channel_title"):
        print(f"CHANNEL: {response['channel_title']}")
    if response.get("display_name"):
        print(
            "DISPLAY: "
            f"{response['display_name']} ({response.get('display_kind', 'unknown')})"
        )
    if response.get("fallback_from"):
        print(f"DISPLAY FALLBACK: {response['fallback_from']}")
    print(f"SUCCESS: {response.get('status')} ({response.get('request_id')})")
    next_target = active_target
    next_surface = active_surface
    next_surface_target = active_surface_target
    if action.action == "media.close":
        next_target = None
        next_surface = None
        next_surface_target = None
    elif action.action not in {"tv.remote", "media.pause", "media.resume"}:
        next_target = action.target
        next_surface_target = action.target
        if action.action in {
            "youtube.play_latest",
            "youtube.search_latest",
            "youtube.search_play",
        } or (
            action.action == "web.open"
            and action.arguments.get("site") == "youtube"
        ):
            next_surface = "youtube"
        elif action.action == "web.open":
            next_surface = str(action.arguments.get("site", "web"))
        elif action.action == "web.search":
            next_surface = "google"
        elif action.action == "app.open":
            next_surface = str(action.arguments.get("app", "app"))
        else:
            next_surface = None
    elif action.action == "tv.remote" and next_surface_target is None:
        next_target = "tv"
    return CommandOutcome(
        active_target=next_target,
        action=action.action,
        target=action.target,
        response=dict(response),
        active_surface=next_surface,
        active_surface_target=next_surface_target,
    )


class CommandSession:
    """Serialize every input through one display and conversational session."""

    def __init__(
        self,
        args: argparse.Namespace,
        state: StateMachine,
        secret: bytes | None,
        latest_resolver,
        search_resolver,
        player: ChromiumHdmiPlayer,
        llm_router: LlmRouter | None,
        *,
        video_resolver=None,
        canvas_state: RoomCanvasState | None = None,
    ) -> None:
        self.args = args
        self.state = state
        self.secret = secret
        self.latest_resolver = latest_resolver
        self.search_resolver = search_resolver
        self.player = player
        self.llm_router = llm_router
        self.video_resolver = video_resolver
        self.canvas_state = canvas_state
        self.active_target: str | None = None
        self.active_surface: str | None = None
        self.active_surface_target: str | None = None
        self._lock = threading.Lock()

    def execute(
        self,
        transcript: str,
        *,
        require_activation: bool | None = None,
    ) -> dict[str, object]:
        with self._lock:
            try:
                outcome = run_attempt(
                    self.args,
                    self.state,
                    self.secret,
                    self.latest_resolver,
                    self.search_resolver,
                    self.player,
                    self.active_target,
                    self.llm_router,
                    active_surface=self.active_surface,
                    active_surface_target=self.active_surface_target,
                    video_resolver=self.video_resolver,
                    transcript_override=transcript,
                    canvas_state=self.canvas_state,
                    require_activation_override=require_activation,
                )
            except (HubError, NoActivation, NoRoute) as exc:
                self.state.transition(AgentState.ERROR, str(exc))
                self.state.transition(AgentState.READY, "waiting for a command")
                raise HubError(str(exc)) from exc
            self.active_target = outcome.active_target
            self.active_surface = outcome.active_surface
            self.active_surface_target = outcome.active_surface_target
            detail = (
                "say pause, continue, or quit"
                if self.active_target is not None
                else "waiting for a command"
            )
            self.state.transition(AgentState.READY, detail)
            response: dict[str, object] = {
                "active_target": self.active_target or "none",
                "action": outcome.action,
                "target": outcome.target,
            }
            for key in ("title", "channel_title", "display_name", "display_kind"):
                if outcome.response.get(key) is not None:
                    response[key] = outcome.response[key]
            return response


def build_iphone_gateway(
    args: argparse.Namespace,
    session: CommandSession,
) -> IphoneCommandServer:
    """Build a revocable, audit-logged gateway for the shared command session."""

    registry = IphoneDeviceRegistry(args.iphone_devices_file.expanduser())
    try:
        server = IphoneCommandServer(
            registry.authenticate,
            lambda transcript: session.execute(
                transcript,
                require_activation=False,
            ),
            pairing_redeemer=registry.redeem_pairing,
            host=args.iphone_bind,
            port=args.iphone_port,
            audit_path=args.iphone_audit_file.expanduser(),
        )
    except (OSError, ValueError, IphoneGatewayError) as exc:
        raise HubError(f"iPhone gateway could not start: {exc}") from exc
    print(
        "IPHONE GATEWAY: ready at "
        f"http://{args.iphone_bind}:{server.port}/v1/commands"
    )
    return server


def main() -> int:
    load_environment_file(Path(__file__).with_name(".env"))
    args = build_parser().parse_args()
    if args.seconds < 2 or args.seconds > 30:
        print("ERROR: --seconds must be between 2 and 30.", file=sys.stderr)
        return 2
    if not 0 <= args.led_brightness <= 31:
        print("ERROR: --led-brightness must be between 0 and 31.", file=sys.stderr)
        return 2
    if args.hands_free and (args.button_mode or args.text or args.text_console):
        print(
            "ERROR: --hands-free cannot be combined with button or text modes.",
            file=sys.stderr,
        )
        return 2
    if args.button_mode and (args.text or args.text_console or args.iphone_gateway):
        print(
            "ERROR: --button-mode cannot be combined with text or iPhone modes.",
            file=sys.stderr,
        )
        return 2
    if args.iphone_gateway and (args.button_mode or args.text or args.text_console):
        print(
            "ERROR: --iphone-gateway cannot be combined with button or text modes.",
            file=sys.stderr,
        )
        return 2
    if not 0.1 <= args.speech_threshold_percent <= 20:
        print("ERROR: --speech-threshold-percent must be between 0.1 and 20.", file=sys.stderr)
        return 2
    if not 0.3 <= args.silence_seconds <= 5:
        print("ERROR: --silence-seconds must be between 0.3 and 5.", file=sys.stderr)
        return 2
    if not 3 <= args.max_utterance_seconds <= 30:
        print("ERROR: --max-utterance-seconds must be between 3 and 30.", file=sys.stderr)
        return 2
    if not 0.5 <= args.llm_timeout <= 30:
        print("ERROR: --llm-timeout must be between 0.5 and 30 seconds.", file=sys.stderr)
        return 2
    if not 0.5 <= args.openrouter_timeout <= 30:
        print(
            "ERROR: --openrouter-timeout must be between 0.5 and 30 seconds.",
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.hermes_timeout <= 60:
        print(
            "ERROR: --hermes-timeout must be between 1 and 60 seconds.",
            file=sys.stderr,
        )
        return 2
    if not 1024 <= args.canvas_port <= 65535:
        print("ERROR: --canvas-port must be between 1024 and 65535.", file=sys.stderr)
        return 2
    if not 1024 <= args.iphone_port <= 65535:
        print("ERROR: --iphone-port must be between 1024 and 65535.", file=sys.stderr)
        return 2
    canvas_server = None
    canvas_state = None
    iphone_server = None
    iphone_thread = None
    if args.room_canvas:
        canvas_server = RoomCanvasServer(port=args.canvas_port)
        try:
            canvas_server.start()
        except (OSError, RuntimeError) as exc:
            print(f"ERROR: Room Canvas could not start: {exc}", file=sys.stderr)
            return 2
        canvas_state = canvas_server.state
    observers = [print_state]
    if canvas_state is not None:
        observers.append(canvas_state.observe_state)
    if not args.no_led:
        try:
            observers.append(ReSpeakerLedObserver(brightness=args.led_brightness))
        except (LedUnavailable, ValueError) as exc:
            print(f"WARNING: LED feedback unavailable: {exc}", file=sys.stderr)
    state = StateMachine(
        observers=observers,
        on_observer_error=print_observer_error,
    )
    try:
        secret = read_key(args.key_file) if args.pc_url else None
        try:
            hermes_api_key = os.getenv("HERMES_API_KEY", "").strip()
            openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
            llm_router = build_semantic_router(
                hermes_url=args.hermes_url,
                hermes_api_key=hermes_api_key,
                hermes_model=args.hermes_model,
                hermes_timeout=args.hermes_timeout,
                openrouter_api_key=openrouter_api_key,
                openrouter_model=args.openrouter_model,
                openrouter_timeout=args.openrouter_timeout,
                local_url=args.llm_url,
                local_timeout=args.llm_timeout,
            )
        except ValueError as exc:
            raise HubError(f"Semantic router configuration is invalid: {exc}") from exc
        youtube_api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
        latest_resolver = None
        search_resolver = None
        video_resolver = None
        if youtube_api_key:
            handle = os.getenv("CINKATKO_HANDLE", "@mitoncz").strip()
            try:
                min_duration = int(os.getenv("MIN_EPISODE_SECONDS", "600"))
            except ValueError as exc:
                raise HubError("MIN_EPISODE_SECONDS must be an integer.") from exc
            if min_duration < 1:
                raise HubError("MIN_EPISODE_SECONDS must be positive.")
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
            video_resolver = lambda query: resolve_video_search(
                youtube_api_key,
                query,
            )
        player = ChromiumHdmiPlayer(
            chromium=args.chromium,
            profile_dir=args.chromium_profile.expanduser(),
            home_url=canvas_server.url if canvas_server is not None else None,
        )
        try:
            display_manager = load_display_manager(
                args.display_config.expanduser(),
                player,
            )
        except DisplayConfigError as exc:
            raise HubError(f"Display configuration is invalid: {exc}") from exc
        if args.display_status:
            print(json.dumps(status_document(display_manager), indent=2))
            return 0
        state.transition(AgentState.READY)
        if canvas_server is not None:
            if display_manager.native_ready(force=True):
                print(
                    "DISPLAY: native smart endpoint ready; keeping the Pi HDMI UI hidden"
                )
            else:
                try:
                    display_manager.show_home()
                except (PiPlayerError, DisplayControlError) as exc:
                    print(f"WARNING: Room Canvas display unavailable: {exc}", file=sys.stderr)
        session = CommandSession(
            args,
            state,
            secret,
            latest_resolver,
            search_resolver,
            display_manager,
            llm_router,
            video_resolver=video_resolver,
            canvas_state=canvas_state,
        )
        if args.iphone_gateway:
            iphone_server = build_iphone_gateway(args, session)
            if not args.hands_free:
                iphone_server.serve_forever()
                return 0
            iphone_thread = threading.Thread(
                target=iphone_server.serve_forever,
                name="iphone-gateway",
                daemon=True,
            )
            iphone_thread.start()
            print("INPUTS: ReSpeaker and iPhone are both ready")
        active_target = None
        active_surface = None
        active_surface_target = None
        while True:
            if args.hands_free and args.iphone_gateway:
                try:
                    # READY means the microphone is armed. Command state begins only
                    # after transcription so an iPhone request can run concurrently
                    # with the ReSpeaker's silence wait.
                    transcript = capture_transcript(args, None)
                except HubError as exc:
                    print(f"WARNING: ReSpeaker input unavailable: {exc}", file=sys.stderr)
                    time.sleep(2)
                    continue
                try:
                    session.execute(transcript)
                except HubError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                continue

            transcript_override = None
            if args.text_console:
                transcript_override = prompt_text_console(active_target)
                if transcript_override is None:
                    state.transition(AgentState.STOPPED, "text console closed")
                    return 0
            try:
                outcome = run_attempt(
                    args,
                    state,
                    secret,
                    latest_resolver,
                    search_resolver,
                    display_manager,
                    active_target,
                    llm_router,
                    active_surface=active_surface,
                    active_surface_target=active_surface_target,
                    video_resolver=video_resolver,
                    transcript_override=transcript_override,
                    canvas_state=canvas_state,
                )
                active_target = outcome.active_target
                active_surface = outcome.active_surface
                active_surface_target = outcome.active_surface_target
            except NoActivation as exc:
                print(f"IGNORED: {exc}")
                state.transition(AgentState.READY, "waiting for Room Agent")
                continue
            except (HubError, NoRoute) as exc:
                state.transition(AgentState.ERROR, str(exc))
                print(f"ERROR: {exc}", file=sys.stderr)
                if args.hands_free and isinstance(exc, NoRoute):
                    state.transition(AgentState.READY, "waiting for another utterance")
                    continue
                if not args.button_mode and not args.text_console:
                    return 2
            else:
                if args.hands_free:
                    detail = (
                        "say pause, continue, or quit"
                        if active_target is not None
                        else "waiting for another utterance"
                    )
                    state.transition(AgentState.READY, detail)
                    continue
                if not args.button_mode and not args.text_console:
                    return 0

            detail = (
                "type another command"
                if args.text_console
                else "press button for next command"
            )
            state.transition(AgentState.READY, detail)
    except (ProtocolError, HubError) as exc:
        state.transition(AgentState.ERROR, str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        state.transition(AgentState.STOPPED, "cancelled")
        print("Cancelled.", file=sys.stderr)
        return 130
    finally:
        if iphone_server is not None:
            iphone_server.shutdown()
            iphone_server.close()
        if iphone_thread is not None:
            iphone_thread.join(timeout=2)
        if canvas_server is not None:
            canvas_server.stop()


if __name__ == "__main__":
    raise SystemExit(main())

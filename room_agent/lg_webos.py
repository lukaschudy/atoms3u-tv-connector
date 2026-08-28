"""Small, bounded LG webOS helper used by the synchronous display manager.

The room-agent service runs with the system Python while the third-party webOS
client lives in the project virtual environment.  This helper is therefore a
separate process with a deliberately small JSON command surface.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable


SSDP_ADDRESS = ("239.255.255.250", 1900)
SSDP_TARGET = "urn:lge-com:service:webos-second-screen:1"
MAX_RESPONSE_BYTES = 65_535
PAIR_TIMEOUT_SECONDS = 90.0
COMMAND_TIMEOUT_SECONDS = 18.0
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class LgWebOsError(RuntimeError):
    pass


def _ssdp_headers(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8", errors="replace")
    except AttributeError as exc:
        raise LgWebOsError("LG discovery returned an invalid packet.") from exc
    headers: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().casefold()] = value.strip()
    return headers


def discover_lg_webos(
    timeout: float = 4.0,
    *,
    socket_factory: Callable[..., Any] = socket.socket,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict[str, str]]:
    """Discover LG webOS second-screen endpoints without enrolling any of them."""

    if not 0.1 <= timeout <= 30.0:
        raise LgWebOsError("LG discovery timeout must be between 0.1 and 30 seconds.")
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {SSDP_TARGET}\r\n\r\n"
    ).encode("ascii")
    found: dict[str, dict[str, str]] = {}
    sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.settimeout(min(0.5, timeout))
        # UDP discovery packets can be lost on otherwise healthy home Wi-Fi.
        sock.sendto(request, SSDP_ADDRESS)
        sock.sendto(request, SSDP_ADDRESS)
        deadline = clock() + timeout
        while clock() < deadline:
            try:
                data, address = sock.recvfrom(MAX_RESPONSE_BYTES)
            except socket.timeout:
                continue
            except OSError as exc:
                raise LgWebOsError(f"LG network discovery failed: {exc}") from exc
            headers = _ssdp_headers(data)
            identity = " ".join(
                headers.get(name, "") for name in ("st", "usn", "server")
            ).casefold()
            if "webos-second-screen" not in identity:
                continue
            host = str(address[0])
            try:
                ipaddress.ip_address(host)
            except ValueError:
                continue
            found[host] = {
                "host": host,
                "location": headers.get("location", ""),
                "usn": headers.get("usn", ""),
            }
    finally:
        sock.close()
    return [found[host] for host in sorted(found, key=ipaddress.ip_address)]


def _compact_apps(apps: Any) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    if not isinstance(apps, list):
        return compact
    for app in apps:
        if not isinstance(app, dict):
            continue
        app_id = app.get("id")
        title = app.get("title")
        if not isinstance(app_id, str) or not _APP_ID.fullmatch(app_id):
            continue
        if not isinstance(title, str) or not title.strip():
            title = app_id
        compact.append(
            {
                "id": app_id,
                "title": title.strip()[:160],
                "visible": bool(app.get("visible", True)),
            }
        )
    return compact


def _allowed_web_url(value: str) -> str:
    if len(value) > 512:
        raise LgWebOsError("The LG browser URL is too long.")
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").casefold()
    allowed = (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and host in {"www.google.com", "www.miton.cz"}
    )
    if not allowed:
        raise LgWebOsError("The LG browser URL is not allowlisted.")
    return value


async def _run_webos(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from bscpylgtv import WebOsClient
    except ImportError as exc:
        raise LgWebOsError(
            "bscpylgtv is not installed in this Python environment; "
            "install room_agent/requirements-pi.txt in .venv."
        ) from exc

    args.key_file.parent.mkdir(parents=True, exist_ok=True)
    client = await WebOsClient.create(
        args.host,
        key_file_path=str(args.key_file),
        ping_interval=None,
        states=[],
        timeout_connect=3,
        connect_retry_attempts=3,
        connect_retry_interval_ms=250,
        without_ssl=args.without_ssl,
    )
    connection_timeout = (
        PAIR_TIMEOUT_SECONDS if args.operation == "pair" else COMMAND_TIMEOUT_SECONDS
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=connection_timeout)
        if args.operation in {"pair", "probe"}:
            apps = _compact_apps(await client.get_apps_all())
            current_app = await client.get_current_app()
            return {
                "status": "paired" if args.operation == "pair" else "ready",
                "host": args.host,
                "app_count": len(apps),
                "current_app": current_app if isinstance(current_app, str) else "",
                "apps": apps,
            }
        if args.operation == "launch-app":
            await client.launch_app(args.app_id)
            return {"status": "executed", "app_id": args.app_id}
        if args.operation == "open-url":
            url = _allowed_web_url(args.url)
            await client.open_url(url)
            return {"status": "executed", "opened": url}
        if args.operation == "youtube":
            await client.launch_app_with_content_id(args.app_id, args.video_id)
            return {
                "status": "executed",
                "app_id": args.app_id,
                "video_id": args.video_id,
            }
        if args.operation == "pause":
            await client.pause()
            return {"status": "executed", "media_status": "PauseRequested"}
        if args.operation == "resume":
            await client.play()
            return {"status": "executed", "media_status": "PlayRequested"}
        if args.operation == "home":
            await client.button("HOME")
            return {"status": "executed", "media_status": "Home"}
        raise LgWebOsError("Unsupported LG operation.")
    except asyncio.TimeoutError as exc:
        raise LgWebOsError(
            "LG TV connection timed out. Keep the TV on and accept its pairing prompt."
        ) from exc
    finally:
        await client.disconnect()


def _valid_host(value: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("LG host must be an IP address.") from exc
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    discover = subparsers.add_parser("discover")
    discover.add_argument("--timeout", type=float, default=4.0)

    def remote_parser(name: str) -> argparse.ArgumentParser:
        command = subparsers.add_parser(name)
        command.add_argument("--host", required=True, type=_valid_host)
        command.add_argument("--key-file", required=True, type=Path)
        command.add_argument(
            "--without-ssl",
            action="store_true",
            help="Use legacy webOS port 3000 instead of secure port 3001.",
        )
        return command

    remote_parser("pair")
    remote_parser("probe")
    launch = remote_parser("launch-app")
    launch.add_argument("app_id", type=lambda value: _validated(_APP_ID, value, "app id"))
    open_url = remote_parser("open-url")
    open_url.add_argument("url")
    youtube = remote_parser("youtube")
    youtube.add_argument("app_id", type=lambda value: _validated(_APP_ID, value, "app id"))
    youtube.add_argument(
        "video_id", type=lambda value: _validated(_VIDEO_ID, value, "YouTube video id")
    )
    remote_parser("pause")
    remote_parser("resume")
    remote_parser("home")
    return parser


def _validated(pattern: re.Pattern[str], value: str, description: str) -> str:
    if not pattern.fullmatch(value):
        raise argparse.ArgumentTypeError(f"Invalid {description}.")
    return value


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.operation == "discover":
            result: Any = {"devices": discover_lg_webos(args.timeout)}
        else:
            args.key_file = args.key_file.expanduser().resolve()
            if args.operation == "pair":
                print(
                    "Approve the Room Agent connection prompt on the LG TV.",
                    file=sys.stderr,
                    flush=True,
                )
            with contextlib.redirect_stdout(sys.stderr):
                result = asyncio.run(_run_webos(args))
            if args.key_file.exists():
                try:
                    os.chmod(args.key_file, 0o600)
                except OSError:
                    pass
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

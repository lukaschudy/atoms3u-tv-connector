"""Restricted Samsung Tizen helper used by the synchronous display manager.

The third-party WebSocket client is isolated in the project virtual environment.
Only a small set of validated operations is exposed to the room-agent service.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any


COMMAND_TIMEOUT_SECONDS = 8.0
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ALLOWED_KEYS = {
    "KEY_HOME",
    "KEY_PAUSE",
    "KEY_PLAY",
}


class SamsungTizenError(RuntimeError):
    pass


def _compact_apps(apps: Any) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    if not isinstance(apps, list):
        return compact
    for app in apps:
        if not isinstance(app, dict):
            continue
        app_id = app.get("appId") or app.get("id")
        title = app.get("name") or app.get("title")
        if not isinstance(app_id, str) or not _APP_ID.fullmatch(app_id):
            continue
        if not isinstance(title, str) or not title.strip():
            title = app_id
        compact.append(
            {
                "id": app_id,
                "title": title.strip()[:160],
                "visible": bool(app.get("isVisible", app.get("visible", True))),
            }
        )
    return compact


def _allowed_web_url(value: str) -> str:
    if len(value) > 512:
        raise SamsungTizenError("The Samsung browser URL is too long.")
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").casefold()
    allowed = (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and host in {"www.google.com", "www.miton.cz", "www.youtube.com"}
    )
    if not allowed:
        raise SamsungTizenError("The Samsung browser URL is not allowlisted.")
    return value


def _valid_host(value: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Samsung host must be an IP address.") from exc
    return value


def _valid_app_id(value: str) -> str:
    if not _APP_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("Invalid Samsung app id.")
    return value


def _valid_video_id(value: str) -> str:
    if not _VIDEO_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("Invalid YouTube video id.")
    return value


def _client(args: argparse.Namespace) -> Any:
    try:
        from samsungtvws import SamsungTVWS
    except ImportError as exc:
        raise SamsungTizenError(
            "samsungtvws is not installed; install room_agent/requirements-pi.txt "
            "in the project .venv."
        ) from exc

    args.token_file.parent.mkdir(parents=True, exist_ok=True)
    return SamsungTVWS(
        host=args.host,
        port=args.port,
        token_file=str(args.token_file),
        timeout=COMMAND_TIMEOUT_SECONDS,
        key_press_delay=0.25,
        name="Room Agent",
    )


def _run(args: argparse.Namespace) -> dict[str, Any]:
    tv = _client(args)
    try:
        tv.open()
        if args.operation == "pair":
            return {
                "status": "paired",
                "host": args.host,
                "port": args.port,
            }
        if args.operation == "probe":
            try:
                apps = _compact_apps(tv.app_list() or [])
                app_list_available = True
            except Exception:
                # Some Tizen generations accept remote keys but do not implement
                # ed.installedApp.get. Keep those controls usable without
                # inventing any app-launch capabilities.
                apps = []
                app_list_available = False
            return {
                "status": "ready",
                "host": args.host,
                "port": args.port,
                "app_count": len(apps),
                "app_list_available": app_list_available,
                "apps": apps,
            }
        if args.operation == "launch-app":
            tv.run_app(args.app_id)
            return {"status": "executed", "app_id": args.app_id}
        if args.operation == "open-url":
            url = _allowed_web_url(args.url)
            tv.open_browser(url)
            return {"status": "executed", "opened": url}
        if args.operation == "youtube":
            url = f"https://www.youtube.com/watch?v={args.video_id}"
            tv.open_browser(url)
            return {
                "status": "executed",
                "opened": url,
                "video_id": args.video_id,
            }
        if args.operation == "key":
            if args.key not in _ALLOWED_KEYS:
                raise SamsungTizenError("Unsupported Samsung remote key.")
            tv.send_key(args.key)
            return {"status": "executed", "key": args.key}
        raise SamsungTizenError("Unsupported Samsung operation.")
    finally:
        tv.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    def remote_parser(name: str) -> argparse.ArgumentParser:
        command = subparsers.add_parser(name)
        command.add_argument("--host", required=True, type=_valid_host)
        command.add_argument("--port", required=True, type=int, choices=(8001, 8002))
        command.add_argument("--token-file", required=True, type=Path)
        return command

    remote_parser("pair")
    remote_parser("probe")
    launch = remote_parser("launch-app")
    launch.add_argument("app_id", type=_valid_app_id)
    open_url = remote_parser("open-url")
    open_url.add_argument("url")
    youtube = remote_parser("youtube")
    youtube.add_argument("video_id", type=_valid_video_id)
    key = remote_parser("key")
    key.add_argument("key", choices=sorted(_ALLOWED_KEYS))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.token_file = args.token_file.expanduser().resolve()
    try:
        if args.operation == "pair":
            print(
                "Approve the Room Agent connection prompt on the Samsung TV.",
                file=sys.stderr,
                flush=True,
            )
        with contextlib.redirect_stdout(sys.stderr):
            result = _run(args)
        if args.token_file.exists():
            try:
                os.chmod(args.token_file, 0o600)
            except OSError:
                pass
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

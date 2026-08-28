"""Enroll trusted native display endpoints without storing pairing secrets."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from display_manager import CONFIG_VERSION, DisplayConfigError, MAX_ENDPOINTS
from lg_webos import LgWebOsError, discover_lg_webos


DEFAULT_CONFIG = Path(__file__).with_name("display_endpoints.json")
DEFAULT_LG_KEY_FILE = Path.home() / ".config" / "room-agent" / "lg-webos.sqlite"
DEFAULT_LG_PYTHON = Path.home() / "room-agent" / ".venv" / "bin" / "python"
DEFAULT_LG_HELPER = Path(__file__).with_name("lg_webos.py")
DEFAULT_SAMSUNG_TOKEN_FILE = (
    Path.home() / ".config" / "room-agent" / "samsung-token.txt"
)
DEFAULT_SAMSUNG_PYTHON = DEFAULT_LG_PYTHON
DEFAULT_SAMSUNG_HELPER = Path(__file__).with_name("samsung_tizen.py")


def _empty_config() -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "settings": {"probe_cache_seconds": 60},
        "endpoints": [],
    }


def read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_config()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DisplayConfigError(f"Existing display configuration is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != CONFIG_VERSION:
        raise DisplayConfigError(
            f"Existing display configuration must use version {CONFIG_VERSION}."
        )
    if not isinstance(payload.get("endpoints"), list):
        raise DisplayConfigError("Existing display endpoints must be a JSON list.")
    payload.setdefault("settings", {"probe_cache_seconds": 60})
    return payload


def write_config(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        Path(temporary_name).replace(path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def upsert_endpoint(path: Path, endpoint: dict[str, Any]) -> None:
    payload = read_config(path)
    endpoints = [
        existing
        for existing in payload["endpoints"]
        if not isinstance(existing, dict) or existing.get("id") != endpoint["id"]
    ]
    endpoints.append(endpoint)
    if len(endpoints) > MAX_ENDPOINTS:
        raise DisplayConfigError(f"At most {MAX_ENDPOINTS} endpoints may be enrolled.")
    payload["endpoints"] = endpoints
    write_config(path, payload)


def remove_endpoint(path: Path, endpoint_id: str) -> bool:
    payload = read_config(path)
    endpoints = payload["endpoints"]
    retained = [
        endpoint
        for endpoint in endpoints
        if not isinstance(endpoint, dict) or endpoint.get("id") != endpoint_id
    ]
    changed = len(retained) != len(endpoints)
    payload["endpoints"] = retained
    if changed:
        write_config(path, payload)
    return changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    android = subparsers.add_parser("android", help="Enroll a paired Google/Android TV.")
    android.add_argument("--id", default="google_tv")
    android.add_argument("--name", default="Google TV")
    android.add_argument("--serial", required=True, help="Paired ADB serial, usually IP:port.")
    android.add_argument("--host", help="Stable IP/host when the ADB port may change.")
    android.add_argument("--adb", default="adb")
    android.add_argument("--priority", type=int, default=100)

    apple = subparsers.add_parser("apple", help="Enroll an already paired Apple TV.")
    apple.add_argument("--id", default="apple_tv")
    apple.add_argument("--name", default="Apple TV")
    apple.add_argument("--host", required=True)
    apple.add_argument("--identifier", required=True)
    apple.add_argument(
        "--atvremote",
        default=str(Path.home() / "room-agent" / ".venv" / "bin" / "atvremote"),
    )
    apple.add_argument("--priority", type=int, default=90)

    samsung = subparsers.add_parser(
        "samsung",
        help="Pair and enroll a modern Samsung Tizen TV.",
    )
    samsung.add_argument("--id", default="samsung_tv")
    samsung.add_argument("--name", default="Samsung TV")
    samsung.add_argument("--host", required=True)
    samsung.add_argument("--port", type=int, choices=(8001, 8002), default=8002)
    samsung.add_argument("--token-file", type=Path, default=DEFAULT_SAMSUNG_TOKEN_FILE)
    samsung.add_argument("--python", type=Path, default=DEFAULT_SAMSUNG_PYTHON)
    samsung.add_argument("--helper", type=Path, default=DEFAULT_SAMSUNG_HELPER)
    samsung.add_argument("--priority", type=int, default=105)

    lg = subparsers.add_parser(
        "lg",
        help="Discover, pair and enroll an LG webOS TV.",
    )
    lg.add_argument("--id", default="lg_tv")
    lg.add_argument("--name", default="LG webOS TV")
    lg.add_argument(
        "--host",
        help="TV IP address; omit it to discover exactly one LG webOS TV.",
    )
    lg.add_argument("--key-file", type=Path, default=DEFAULT_LG_KEY_FILE)
    lg.add_argument("--python", type=Path, default=DEFAULT_LG_PYTHON)
    lg.add_argument("--helper", type=Path, default=DEFAULT_LG_HELPER)
    lg.add_argument(
        "--without-ssl",
        action="store_true",
        help="Use legacy webOS port 3000 when secure port 3001 is unavailable.",
    )
    lg.add_argument("--discovery-timeout", type=float, default=4.0)
    lg.add_argument("--priority", type=int, default=110)

    remove = subparsers.add_parser("remove", help="Remove one enrolled endpoint.")
    remove.add_argument("id")
    subparsers.add_parser("show", help="Print the non-secret enrollment file.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    path = args.config.expanduser()
    try:
        if args.command == "show":
            print(json.dumps(read_config(path), indent=2, ensure_ascii=False))
            return 0
        if args.command == "remove":
            changed = remove_endpoint(path, args.id)
            print("Removed." if changed else "No matching endpoint was enrolled.")
            return 0
        if args.command == "lg":
            host = args.host
            if host is None:
                devices = discover_lg_webos(args.discovery_timeout)
                if not devices:
                    raise DisplayConfigError(
                        "No LG webOS TV was discovered. Keep it on and on the same "
                        "LAN, or rerun with --host TV_IP."
                    )
                if len(devices) > 1:
                    hosts = ", ".join(device["host"] for device in devices)
                    raise DisplayConfigError(
                        f"Multiple LG webOS TVs were discovered ({hosts}); "
                        "rerun with --host for the intended TV."
                    )
                host = devices[0]["host"]
                print(f"Discovered one LG webOS TV at {host}.")
            key_file = args.key_file.expanduser().resolve()
            python = args.python.expanduser().resolve()
            helper = args.helper.expanduser().resolve()
            print("A connection prompt should appear on the TV; approve Room Agent.")
            try:
                pairing_command = [
                    str(python),
                    str(helper),
                    "pair",
                    "--host",
                    host,
                    "--key-file",
                    str(key_file),
                ]
                if args.without_ssl:
                    pairing_command.append("--without-ssl")
                pairing = subprocess.run(
                    pairing_command,
                    text=True,
                    timeout=100,
                )
            except FileNotFoundError as exc:
                raise DisplayConfigError(
                    f"LG helper Python was not found: {python}. Install the Pi "
                    "requirements in ~/room-agent/.venv first."
                ) from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise DisplayConfigError(f"LG pairing could not run: {exc}") from exc
            if pairing.returncode != 0:
                raise DisplayConfigError(
                    "LG pairing failed. Confirm that the TV is on and approve its prompt."
                )
        elif args.command == "samsung":
            token_file = args.token_file.expanduser().resolve()
            python = args.python.expanduser().resolve()
            helper = args.helper.expanduser().resolve()
            print("A connection prompt should appear on the TV; approve Room Agent.")
            try:
                pairing = subprocess.run(
                    [
                        str(python),
                        str(helper),
                        "pair",
                        "--host",
                        args.host,
                        "--port",
                        str(args.port),
                        "--token-file",
                        str(token_file),
                    ],
                    text=True,
                    timeout=100,
                )
            except FileNotFoundError as exc:
                raise DisplayConfigError(
                    f"Samsung helper Python was not found: {python}. Install the Pi "
                    "requirements in ~/room-agent/.venv first."
                ) from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise DisplayConfigError(f"Samsung pairing could not run: {exc}") from exc
            if pairing.returncode != 0:
                raise DisplayConfigError(
                    "Samsung pairing failed. Confirm that the TV is on, local network "
                    "control is allowed, and approve its prompt."
                )

        if args.command == "android":
            kind = "android_tv"
        elif args.command == "lg":
            kind = "lg_webos"
        else:
            kind = f"{args.command}_tv"
        common = {
            "id": args.id,
            "name": args.name,
            "kind": kind,
            "priority": args.priority,
            "enabled": True,
        }
        if args.command == "android":
            endpoint = {
                **common,
                "serial": args.serial,
                "host": args.host or args.serial.rsplit(":", 1)[0],
                "adb": args.adb,
                "connect_on_probe": True,
                "apps": {
                    "youtube": "com.google.android.youtube.tv",
                    "netflix": "com.netflix.ninja"
                },
            }
        elif args.command == "apple":
            endpoint = {
                **common,
                "host": args.host,
                "identifier": args.identifier,
                "atvremote": args.atvremote,
            }
        elif args.command == "lg":
            endpoint = {
                **common,
                "host": host,
                "key_file": str(key_file),
                "python": str(python),
                "helper": str(helper),
                "without_ssl": args.without_ssl,
            }
        elif args.command == "samsung":
            endpoint = {
                **common,
                "host": args.host,
                "port": args.port,
                "token_file": str(token_file),
                "python": str(python),
                "helper": str(helper),
            }
        else:
            endpoint = {**common, "host": args.host}
        upsert_endpoint(path, endpoint)
    except (DisplayConfigError, LgWebOsError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"Enrolled {endpoint['name']} in {path}.")
    print("Pairing credentials remain in the platform tool's private storage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

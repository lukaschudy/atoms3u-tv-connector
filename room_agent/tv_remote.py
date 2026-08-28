"""Small command-line remote for an enrolled ATOMS3U TV connector."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Any

from configure_connector import discover_connectors
from protocol import ProtocolError, read_key
from setup_tv_connector import configured_profile
from tv_connector import (
    SUPPORTED_CONNECTOR_PROFILES,
    TvConnectorError,
    UsbTvConnectorClient,
)


DEFAULT_KEY_FILE = Path(__file__).resolve().parent / "tv-connector.key"
REMOTE_KEYS = (
    "POWER",
    "POWER_ON",
    "POWER_OFF",
    "HOME",
    "BACK",
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
    "ENTER",
    "PLAY",
    "PAUSE",
    "PLAY_PAUSE",
    "MUTE",
    "VOLUME_UP",
    "VOLUME_DOWN",
    "INPUT",
    "SEARCH",
    "NETFLIX",
)


def dispatch(client: UsbTvConnectorClient, command: str, *, count: int = 1) -> dict[str, Any]:
    """Execute one bounded remote command through the authenticated client."""

    if command == "status":
        return client.health()
    return client.press_key(command, count=count)


def resolve_connector_url(
    url: str | None,
    *,
    profile: str,
    timeout: float = 5.0,
    discover: Callable[[float], list[dict[str, Any]]] = discover_connectors,
) -> str:
    """Use an explicit URL or safely select one discovered matching connector."""

    if url:
        return url
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise ProtocolError("Discovery timeout must be between 0 and 60 seconds.")
    matches = [record for record in discover(timeout) if record.get("profile") == profile]
    if not matches:
        raise ProtocolError(
            "No connector with the configured profile was discovered. "
            "Cold-boot it on the same network or pass --url."
        )
    if len(matches) > 1:
        raise ProtocolError("More than one matching connector was found; pass --url.")
    return str(matches[0]["url"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a bounded, authenticated command to an ATOMS3U TV connector."
    )
    parser.add_argument("--url", help="Connector origin, for example http://192.168.1.50")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument(
        "--profile",
        choices=SUPPORTED_CONNECTOR_PROFILES,
        help="Defaults to the profile selected during setup.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Listen for connectors on the LAN.")
    discover.add_argument("--timeout", type=float, default=10.0)

    subparsers.add_parser("status", help="Show connector readiness and transport health.")
    press = subparsers.add_parser("press", help="Press one allowlisted remote key.")
    press.add_argument("key", choices=REMOTE_KEYS, type=str.upper)
    press.add_argument("--count", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "discover":
            connectors = discover_connectors(args.timeout)
            if not connectors:
                print("No connector found. Confirm 2.4 GHz Wi-Fi and firewall settings.")
                return 1
            print(json.dumps(connectors, indent=2))
            return 0

        profile = args.profile or configured_profile()
        url = resolve_connector_url(args.url, profile=profile)
        client = UsbTvConnectorClient(
            url,
            read_key(args.key_file),
            profile=profile,
        )
        command = "status" if args.command == "status" else args.key
        result = dispatch(client, command, count=getattr(args, "count", 1))
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ProtocolError, TvConnectorError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Prepare and verify a Room Agent USB TV connector enrollment."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import re
import socket
import time
from pathlib import Path

from protocol import (
    ProtocolError,
    generate_key,
    read_key,
    read_private_text,
    write_private_text,
)
from tv_connector import (
    DEFAULT_CONNECTOR_BLE_NAME,
    DEFAULT_CONNECTOR_PROFILE,
    BleTvConnectorClient,
    SUPPORTED_CONNECTOR_PROFILES,
    TvConnectorError,
    UsbTvConnectorClient,
)


_FIRMWARE_KEY = re.compile(
    r'(#define\s+ROOM_AGENT_CONNECTOR_KEY_HEX\s+(?:\\\s*)?")'
    r'([0-9A-Fa-f]{64})(")'
)
_CPP_STRING_CONTENT = r'(?:\\.|[^"\\])*'
_FIRMWARE_SSID = re.compile(
    rf'(#define\s+ROOM_AGENT_WIFI_SSID\s+")({_CPP_STRING_CONTENT})(")'
)
_FIRMWARE_PASSWORD = re.compile(
    rf'(#define\s+ROOM_AGENT_WIFI_PASSWORD\s+")({_CPP_STRING_CONTENT})(")'
)
_FIRMWARE_PROFILE = re.compile(
    r'(#define\s+ROOM_AGENT_CONNECTOR_PROFILE\s+")([^"]*)(")'
)
_DISCOVERY_MAGIC = "room-agent-tv-connector"
_DISCOVERY_PORT = 8781
_ENV_KEY = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def discover_connectors(
    timeout_seconds: float = 10.0,
    *,
    port: int = _DISCOVERY_PORT,
    bind_host: str = "",
) -> list[dict[str, object]]:
    """Listen for bounded, unauthenticated connector discovery hints."""

    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > 60
    ):
        raise ProtocolError("Discovery timeout must be between 0 and 60 seconds.")
    if port < 1 or port > 65535:
        raise ProtocolError("Discovery port must be between 1 and 65535.")
    found: dict[tuple[str, int, str], dict[str, object]] = {}
    deadline = time.monotonic() + timeout_seconds
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((bind_host, port))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            listener.settimeout(remaining)
            try:
                payload, source = listener.recvfrom(2048)
            except socket.timeout:
                break
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            name = message.get("name")
            profile = message.get("profile")
            announced_port = message.get("port")
            if (
                message.get("magic") != _DISCOVERY_MAGIC
                or message.get("version") != 1
                or not isinstance(name, str)
                or not 1 <= len(name) <= 64
                or not isinstance(profile, str)
                or not 1 <= len(profile) <= 64
                or not isinstance(announced_port, int)
                or not 1 <= announced_port <= 65535
            ):
                continue
            source_ip = source[0]
            record: dict[str, object] = {
                "name": name,
                "profile": profile,
                "ip": source_ip,
                "port": announced_port,
                "url": f"http://{source_ip}:{announced_port}",
            }
            rssi = message.get("wifi_rssi")
            if isinstance(rssi, int):
                record["wifi_rssi"] = rssi
            found[(source_ip, announced_port, name)] = record
    except OSError as exc:
        raise ProtocolError(f"Connector discovery failed: {exc}") from exc
    finally:
        listener.close()
    return sorted(found.values(), key=lambda record: (str(record["name"]), str(record["ip"])))


def _printable_wifi_value(value: str, *, name: str, minimum: int, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if not minimum <= len(encoded) <= maximum:
        raise ProtocolError(f"{name} must contain {minimum} to {maximum} UTF-8 bytes.")
    if any(character < 0x20 or character > 0x7E for character in encoded):
        raise ProtocolError(f"{name} must contain printable ASCII characters.")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _replace_private_text(path: Path, content: str, *, description: str) -> None:
    write_private_text(path, content, description=description)


def _read_private_configuration(path: Path, *, description: str) -> str:
    return read_private_text(path, description=description)


def provision_firmware_network(ssid: str, password: str, secrets_file: Path) -> None:
    """Store Wi-Fi settings in the ignored firmware header without printing them."""

    escaped_ssid = _printable_wifi_value(
        ssid, name="Wi-Fi SSID", minimum=1, maximum=32
    )
    escaped_password = _printable_wifi_value(
        password, name="Wi-Fi password", minimum=8, maximum=63
    )
    content = _read_private_configuration(
        secrets_file,
        description="firmware secrets",
    )
    updated, ssid_replacements = _FIRMWARE_SSID.subn(
        lambda match: f"{match.group(1)}{escaped_ssid}{match.group(3)}", content
    )
    updated, password_replacements = _FIRMWARE_PASSWORD.subn(
        lambda match: f"{match.group(1)}{escaped_password}{match.group(3)}", updated
    )
    if ssid_replacements != 1 or password_replacements != 1:
        raise ProtocolError(
            "Firmware secrets must contain exactly one Wi-Fi SSID and password."
        )
    _replace_private_text(secrets_file, updated, description="firmware secrets")


def _validate_profile(profile: str) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", profile) is None:
        raise ProtocolError("TV connector profile name is invalid.")
    if profile not in SUPPORTED_CONNECTOR_PROFILES:
        raise ProtocolError("TV connector profile is unsupported.")
    return profile


def configure_firmware_profile(profile: str, secrets_file: Path) -> None:
    """Select a bounded firmware profile without exposing other secrets."""

    profile = _validate_profile(profile)
    content = _read_private_configuration(
        secrets_file,
        description="firmware secrets",
    )
    updated, replacements = _FIRMWARE_PROFILE.subn(
        lambda match: f"{match.group(1)}{profile}{match.group(3)}",
        content,
    )
    if replacements != 1:
        raise ProtocolError(
            "Firmware secrets must contain exactly one "
            "ROOM_AGENT_CONNECTOR_PROFILE value."
        )
    _replace_private_text(secrets_file, updated, description="firmware profile")


def sync_firmware_key(key_file: Path, secrets_file: Path) -> None:
    """Copy the private connector key into the ignored firmware header."""

    key_hex = read_key(key_file).hex()
    content = _read_private_configuration(
        secrets_file,
        description="firmware secrets",
    )
    updated, replacements = _FIRMWARE_KEY.subn(
        lambda match: f"{match.group(1)}{key_hex}{match.group(3)}",
        content,
    )
    if replacements != 1:
        raise ProtocolError(
            "Firmware secrets must contain exactly one 64-character "
            "ROOM_AGENT_CONNECTOR_KEY_HEX value."
        )
    _replace_private_text(secrets_file, updated, description="firmware secrets")


def configure_ble_host(
    env_file: Path,
    key_file: Path,
    device_name: str,
    profile: str,
) -> None:
    """Reject the legacy BLE path until it implements the protocol-v2 contract."""

    raise ProtocolError(
        "Bluetooth connector enrollment is disabled until protocol v2 response "
        "authentication is implemented; use the production Wi-Fi transport."
    )


def configure_wifi_host(
    env_file: Path,
    key_file: Path,
    url: str,
    profile: str,
) -> None:
    """Select the Wi-Fi connector without exposing or replacing other secrets."""

    client = UsbTvConnectorClient(url, read_key(key_file), profile=profile)
    _update_host_environment(
        env_file,
        {
            "TV_CONNECTOR_BLE_NAME": "",
            "TV_CONNECTOR_URL": client.base_url,
            "TV_CONNECTOR_KEY_FILE": str(key_file.resolve()),
            "TV_CONNECTOR_PROFILE": profile,
        },
    )


def _update_host_environment(env_file: Path, updates: dict[str, str]) -> None:
    """Atomically replace only named Room Agent environment values."""

    try:
        env_file.stat(follow_symlinks=False)
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise ProtocolError(f"Could not read host environment file: {exc}") from exc
    else:
        lines = read_private_text(
            env_file,
            require_private=False,
            description="host environment file",
        ).splitlines()
    written: set[str] = set()
    result: list[str] = []
    for line in lines:
        match = _ENV_KEY.match(line)
        key = match.group(1) if match else None
        if key in updates:
            if key not in written:
                result.append(f"{key}={updates[key]}")
                written.add(key)
        else:
            result.append(line)
    if result and result[-1] != "":
        result.append("")
    for key, value in updates.items():
        if key not in written:
            result.append(f"{key}={value}")
    _replace_private_text(
        env_file,
        "\n".join(result) + "\n",
        description="host environment file",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    initialize = subparsers.add_parser("init")
    initialize.add_argument("--key-file", type=Path, default=Path("tv-connector.key"))

    firmware = subparsers.add_parser("sync-firmware-key")
    firmware.add_argument("--key-file", type=Path, default=Path("tv-connector.key"))
    firmware.add_argument(
        "--secrets-file",
        type=Path,
        default=Path("firmware/atoms3u_tv_connector/include/secrets.h"),
    )

    firmware_profile = subparsers.add_parser("set-firmware-profile")
    firmware_profile.add_argument(
        "--profile",
        required=True,
        choices=SUPPORTED_CONNECTOR_PROFILES,
    )
    firmware_profile.add_argument(
        "--secrets-file",
        type=Path,
        default=Path("firmware/atoms3u_tv_connector/include/secrets.h"),
    )

    provision = subparsers.add_parser("provision-firmware")
    provision.add_argument("--ssid")
    provision.add_argument(
        "--password-env",
        help="Read the Wi-Fi password from this environment variable.",
    )
    provision.add_argument("--key-file", type=Path, default=Path("tv-connector.key"))
    provision.add_argument(
        "--secrets-file",
        type=Path,
        default=Path("firmware/atoms3u_tv_connector/include/secrets.h"),
    )

    status = subparsers.add_parser("status")
    status.add_argument("--url", required=True)
    status.add_argument("--key-file", type=Path, default=Path("tv-connector.key"))
    status.add_argument(
        "--profile", default=DEFAULT_CONNECTOR_PROFILE,
        choices=SUPPORTED_CONNECTOR_PROFILES,
    )

    status_ble = subparsers.add_parser("status-ble")
    status_ble.add_argument("--name", default=DEFAULT_CONNECTOR_BLE_NAME)
    status_ble.add_argument("--key-file", type=Path, default=Path("tv-connector.key"))
    status_ble.add_argument(
        "--profile", default=DEFAULT_CONNECTOR_PROFILE,
        choices=(DEFAULT_CONNECTOR_PROFILE,),
    )

    enable_ble = subparsers.add_parser("enable-ble-host")
    enable_ble.add_argument("--name", default=DEFAULT_CONNECTOR_BLE_NAME)
    enable_ble.add_argument("--key-file", type=Path, default=Path("tv-connector.key"))
    enable_ble.add_argument(
        "--profile", default=DEFAULT_CONNECTOR_PROFILE,
        choices=(DEFAULT_CONNECTOR_PROFILE,),
    )
    enable_ble.add_argument("--env-file", type=Path, default=Path(".env"))

    enable_wifi = subparsers.add_parser("enable-wifi-host")
    enable_wifi.add_argument("--url", required=True)
    enable_wifi.add_argument("--key-file", type=Path, default=Path("tv-connector.key"))
    enable_wifi.add_argument(
        "--profile", default=DEFAULT_CONNECTOR_PROFILE,
        choices=SUPPORTED_CONNECTOR_PROFILES,
    )
    enable_wifi.add_argument("--env-file", type=Path, default=Path(".env"))

    discover = subparsers.add_parser("discover")
    discover.add_argument("--timeout", type=float, default=10.0)
    discover.add_argument("--port", type=int, default=_DISCOVERY_PORT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.operation == "init":
            generate_key(args.key_file)
            print(f"Created private connector key: {args.key_file.resolve()}")
            print("The file is excluded from Git. Do not paste its contents into chat.")
            return 0

        if args.operation == "sync-firmware-key":
            sync_firmware_key(args.key_file, args.secrets_file)
            print(f"Synchronized the private key into: {args.secrets_file.resolve()}")
            print("The key was not displayed and both files are excluded from Git.")
            return 0

        if args.operation == "set-firmware-profile":
            configure_firmware_profile(args.profile, args.secrets_file)
            print(f"Selected firmware profile {args.profile!r}.")
            print("Existing Wi-Fi credentials and connector key were preserved.")
            return 0

        if args.operation == "provision-firmware":
            ssid = args.ssid or input("Wi-Fi SSID: ").strip()
            if args.password_env:
                password = os.environ.get(args.password_env)
                if password is None:
                    raise ProtocolError(
                        f"Environment variable {args.password_env!r} is not set."
                    )
            else:
                password = getpass.getpass("Wi-Fi password (hidden): ")
            provision_firmware_network(ssid, password, args.secrets_file)
            sync_firmware_key(args.key_file, args.secrets_file)
            print(f"Provisioned private firmware settings: {args.secrets_file.resolve()}")
            print("The Wi-Fi password and connector key were not displayed.")
            return 0

        if args.operation == "discover":
            connectors = discover_connectors(args.timeout, port=args.port)
            if not connectors:
                print("No connector announcement was received.")
                return 1
            print(json.dumps(connectors, indent=2))
            return 0

        if args.operation == "status-ble":
            client = BleTvConnectorClient(
                args.name,
                read_key(args.key_file),
                profile=args.profile,
            )
            print(json.dumps(client.health(), indent=2))
            return 0

        if args.operation == "enable-ble-host":
            configure_ble_host(
                args.env_file,
                args.key_file,
                args.name,
                args.profile,
            )
            print(f"Enabled Bluetooth TV connector {args.name!r} for the host.")
            print("Existing API keys and other private settings were preserved.")
            return 0

        if args.operation == "enable-wifi-host":
            client = UsbTvConnectorClient(
                args.url,
                read_key(args.key_file),
                profile=args.profile,
            )
            client.verify()
            configure_wifi_host(
                args.env_file,
                args.key_file,
                args.url,
                args.profile,
            )
            print(f"Enabled Wi-Fi TV connector {args.url!r} for the host.")
            print("Existing API keys and other private settings were preserved.")
            return 0

        client = UsbTvConnectorClient(
            args.url,
            read_key(args.key_file),
            profile=args.profile,
        )
        print(json.dumps(client.health(), indent=2))
        return 0
    except (OSError, ProtocolError, TvConnectorError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

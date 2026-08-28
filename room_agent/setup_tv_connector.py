"""One-command preparation, diagnostics, build, and flashing for the TV connector."""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from configure_connector import (
    configure_firmware_profile,
    configure_wifi_host,
    discover_connectors,
    provision_firmware_network,
    sync_firmware_key,
)
from protocol import (
    ProtocolError,
    generate_key,
    read_key,
    read_private_text,
    write_private_text,
)
from tv_connector import SUPPORTED_CONNECTOR_PROFILES, TvConnectorError, UsbTvConnectorClient


HERE = Path(__file__).resolve().parent
FIRMWARE_DIR = HERE / "firmware" / "atoms3u_tv_connector"
DEFAULT_KEY_FILE = HERE / "tv-connector.key"
DEFAULT_SECRETS_FILE = FIRMWARE_DIR / "include" / "secrets.h"
DEFAULT_TEMPLATE_FILE = FIRMWARE_DIR / "include" / "secrets.example.h"
PROFILE_HELP = {
    "generic-usb-hid-v1": "USB keyboard/media keys (TV must support USB HID)",
    "sony-bravia-ir-v1": "Sony Bravia infrared remote",
    "lg-tv-ir-v1": "LG infrared remote",
    "lg-webos-hybrid-v1": "LG infrared plus USB search/text entry",
}
_PROFILE_PATTERN = re.compile(r'#define\s+ROOM_AGENT_CONNECTOR_PROFILE\s+"([^"]*)"')
_CPP_STRING_CONTENT = r'(?:\\.|[^"\\])*'
_SSID_PATTERN = re.compile(
    rf'#define\s+ROOM_AGENT_WIFI_SSID\s+"({_CPP_STRING_CONTENT})"'
)
_PASSWORD_PATTERN = re.compile(
    rf'#define\s+ROOM_AGENT_WIFI_PASSWORD\s+"({_CPP_STRING_CONTENT})"'
)
_KEY_PATTERN = re.compile(
    r'#define\s+ROOM_AGENT_CONNECTOR_KEY_HEX\s+(?:\\\s*)?"([0-9A-Fa-f]{64})"'
)
_PASSWORD_PLACEHOLDERS = {"change-me", "replace-me", "your-wifi-password"}


def _decode_cpp_string(value: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            decoded.append(character)
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {'"', "\\"}:
            return ""
        decoded.append(value[index + 1])
        index += 2
    return "".join(decoded)


def _choose_profile() -> str:
    print("Choose how the ATOMS3U will control the TV:")
    for index, profile in enumerate(SUPPORTED_CONNECTOR_PROFILES, start=1):
        print(f"  {index}. {PROFILE_HELP[profile]}")
    while True:
        selected = input(f"Profile [1-{len(SUPPORTED_CONNECTOR_PROFILES)}]: ").strip()
        try:
            index = int(selected)
            if not 1 <= index <= len(SUPPORTED_CONNECTOR_PROFILES):
                raise ValueError
            return SUPPORTED_CONNECTOR_PROFILES[index - 1]
        except ValueError:
            print("Enter one of the numbers shown above.")


def prepare_connector(
    *,
    profile: str,
    ssid: str,
    password: str,
    key_file: Path = DEFAULT_KEY_FILE,
    secrets_file: Path = DEFAULT_SECRETS_FILE,
    template_file: Path = DEFAULT_TEMPLATE_FILE,
) -> tuple[bool, bool]:
    """Create private files when needed and safely update connector settings."""

    if profile not in SUPPORTED_CONNECTOR_PROFILES:
        raise ProtocolError("TV connector profile is unsupported.")
    if not template_file.is_file():
        raise ProtocolError(f"Firmware settings template is missing: {template_file}")

    created_key = not key_file.exists()
    created_secrets = not secrets_file.exists()
    if not created_key:
        if key_file.is_symlink():
            raise ProtocolError("The existing connector key must not be a symbolic link.")
        existing_key = read_key(key_file, require_private=False)
        write_private_text(
            key_file,
            existing_key.hex() + "\n",
            replace=True,
            description="existing connector key migration",
        )
        read_key(key_file)

    key_file.parent.mkdir(parents=True, exist_ok=True)
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    transaction = uuid.uuid4().hex
    temporary_key = key_file.with_name(f".{key_file.name}.{transaction}.tmp")
    temporary_secrets = secrets_file.with_name(f".{secrets_file.name}.{transaction}.tmp")
    working_key = temporary_key if created_key else key_file
    key_committed = False
    try:
        if created_key:
            generate_key(temporary_key)
        if created_secrets:
            source_content = template_file.read_text(encoding="utf-8")
        else:
            source_content = read_private_text(
                secrets_file,
                require_private=False,
                description="existing firmware settings",
            )
        write_private_text(
            temporary_secrets,
            source_content,
            replace=False,
            description="firmware settings transaction",
        )
        provision_firmware_network(ssid, password, temporary_secrets)
        sync_firmware_key(working_key, temporary_secrets)
        configure_firmware_profile(profile, temporary_secrets)
        if created_key:
            temporary_key.replace(key_file)
            key_committed = True
        temporary_secrets.replace(secrets_file)
    except (OSError, UnicodeError, ProtocolError) as exc:
        if key_committed:
            try:
                key_file.unlink(missing_ok=True)
            except OSError:
                pass
        raise ProtocolError(f"Could not update private connector settings: {exc}") from exc
    finally:
        for temporary in (temporary_key, temporary_secrets):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return created_key, created_secrets


def inspect_configuration(
    *,
    key_file: Path = DEFAULT_KEY_FILE,
    secrets_file: Path = DEFAULT_SECRETS_FILE,
) -> list[tuple[bool, str]]:
    """Return safe readiness checks without exposing credentials."""

    checks: list[tuple[bool, str]] = []
    try:
        key = read_key(key_file)
    except (ProtocolError, UnicodeError):
        key = None
    checks.append(
        (
            key is not None,
            "private connector key exists, is valid, and has owner-only permissions",
        )
    )

    try:
        content = read_private_text(
            secrets_file,
            description="firmware settings",
        )
    except (OSError, UnicodeError, ProtocolError):
        content = ""
    checks.append(
        (
            bool(content),
            "private firmware settings file exists and has owner-only permissions",
        )
    )

    profile_match = _PROFILE_PATTERN.search(content)
    profile = profile_match.group(1) if profile_match else ""
    checks.append((profile in SUPPORTED_CONNECTOR_PROFILES, "firmware profile is supported"))

    ssid_match = _SSID_PATTERN.search(content)
    ssid = _decode_cpp_string(ssid_match.group(1)) if ssid_match else ""
    checks.append((bool(ssid) and ssid != "your-2.4-ghz-wifi", "Wi-Fi SSID is configured"))

    password_match = _PASSWORD_PATTERN.search(content)
    password = _decode_cpp_string(password_match.group(1)) if password_match else ""
    checks.append(
        (
            bool(password) and password.casefold() not in _PASSWORD_PLACEHOLDERS,
            "Wi-Fi password is configured",
        )
    )

    header_key_match = _KEY_PATTERN.search(content)
    header_key = bytes.fromhex(header_key_match.group(1)) if header_key_match else None
    checks.append(
        (
            key is not None and header_key == key and header_key != bytes(32),
            "host and firmware authentication keys match",
        )
    )
    checks.append((_platformio_available(), "PlatformIO is installed"))
    return checks


def configured_profile(secrets_file: Path = DEFAULT_SECRETS_FILE) -> str:
    """Read the non-secret selected profile from the private firmware header."""

    try:
        content = read_private_text(
            secrets_file,
            description="firmware settings",
        )
    except ProtocolError as exc:
        raise ProtocolError("Private firmware settings are missing; run prepare first.") from exc
    match = _PROFILE_PATTERN.search(content)
    profile = match.group(1) if match else ""
    if profile not in SUPPORTED_CONNECTOR_PROFILES:
        raise ProtocolError("The configured firmware profile is missing or unsupported.")
    return profile


def connect_host(
    *,
    url: str | None = None,
    timeout: float = 10.0,
    key_file: Path = DEFAULT_KEY_FILE,
    secrets_file: Path = DEFAULT_SECRETS_FILE,
    env_file: Path = HERE / ".env",
) -> tuple[str, str]:
    """Verify one connector and save it for the Windows voice host."""

    profile = configured_profile(secrets_file)
    key = read_key(key_file)
    if url is None:
        matches = [
            record
            for record in discover_connectors(timeout)
            if record.get("profile") == profile
        ]
        if not matches:
            raise ProtocolError(
                "No connector with the configured profile was discovered. "
                "Cold-boot it on the same network or pass --url."
            )
        if len(matches) > 1:
            raise ProtocolError("More than one matching connector was found; pass --url.")
        url = str(matches[0]["url"])

    client = UsbTvConnectorClient(url, key, profile=profile)
    health = client.health()
    if health.get("profile") != profile:
        raise ProtocolError("The connector profile does not match the local firmware settings.")
    client.verify()
    configure_wifi_host(env_file, key_file, url, profile)
    return client.base_url, profile


def _platformio_available() -> bool:
    return importlib.util.find_spec("platformio") is not None or shutil.which("pio") is not None


def _platformio_command() -> list[str]:
    if importlib.util.find_spec("platformio") is not None:
        return [sys.executable, "-m", "platformio"]
    executable = shutil.which("pio")
    if executable:
        return [executable]
    raise ProtocolError(
        "PlatformIO is not installed. Run the setup script in scripts/ first."
    )


def run_platformio(*, emitter: str, upload: bool, port: str | None = None) -> int:
    if emitter not in {"built-in", "u002"}:
        raise ProtocolError("Emitter must be 'built-in' or 'u002'.")
    environment = (
        "atoms3u-tv-connector-u002" if emitter == "u002" else "atoms3u-tv-connector"
    )
    platformio = _platformio_command()
    clean_command = platformio + [
        "run",
        "-d",
        str(FIRMWARE_DIR),
        "-e",
        environment,
        "-t",
        "clean",
    ]
    clean_result = subprocess.run(clean_command, check=False)
    if clean_result.returncode != 0:
        return clean_result.returncode
    command = platformio + ["run", "-d", str(FIRMWARE_DIR), "-e", environment]
    if upload:
        command.extend(["-t", "upload"])
        if port:
            command.extend(["--upload-port", port])
    return subprocess.run(command, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare, check, build, and flash an ATOMS3U TV connector."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Create private settings interactively.")
    prepare.add_argument("--profile", choices=SUPPORTED_CONNECTOR_PROFILES)
    prepare.add_argument("--ssid", help="2.4 GHz Wi-Fi network name.")
    prepare.add_argument(
        "--password-env",
        help="Read the Wi-Fi password from this environment variable (automation only).",
    )
    prepare.add_argument("--non-interactive", action="store_true")
    prepare.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    prepare.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    prepare.add_argument("--template-file", type=Path, default=DEFAULT_TEMPLATE_FILE)

    doctor = subparsers.add_parser("doctor", help="Check readiness without printing secrets.")
    doctor.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    doctor.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)

    connect = subparsers.add_parser(
        "connect-host",
        help="Discover and save the flashed connector for iPhone voice control.",
    )
    connect.add_argument("--url", help="Connector URL when UDP discovery is unavailable.")
    connect.add_argument("--timeout", type=float, default=10.0)
    connect.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    connect.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    connect.add_argument("--env-file", type=Path, default=HERE / ".env")

    for name, help_text in (
        ("build", "Compile the production firmware."),
        ("flash", "Compile and upload the production firmware."),
    ):
        operation = subparsers.add_parser(name, help=help_text)
        operation.add_argument(
            "--emitter",
            choices=("built-in", "u002"),
            default="built-in",
            help="Physical infrared emitter; the default uses the ATOMS3U side emitter.",
        )
        if name == "flash":
            operation.add_argument("--port", help="Optional serial port override, such as COM5.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            profile = args.profile
            ssid = args.ssid
            if args.non_interactive and (not profile or not ssid or not args.password_env):
                raise ProtocolError(
                    "Non-interactive setup requires --profile, --ssid, and --password-env."
                )
            if not profile:
                profile = _choose_profile()
            if not ssid:
                ssid = input("2.4 GHz Wi-Fi network name: ").strip()
            if args.password_env:
                password = os.environ.get(args.password_env)
                if password is None:
                    raise ProtocolError(
                        f"Environment variable {args.password_env!r} is not set."
                    )
            else:
                password = getpass.getpass("Wi-Fi password (hidden): ")
            created_key, created_secrets = prepare_connector(
                profile=profile,
                ssid=ssid,
                password=password,
                key_file=args.key_file,
                secrets_file=args.secrets_file,
                template_file=args.template_file,
            )
            print("TV connector settings are ready.")
            print(f"  Profile: {profile}")
            print(f"  Key: {'created' if created_key else 'reused'} (contents hidden)")
            print(
                f"  Firmware settings: {'created' if created_secrets else 'updated'} "
                "(credentials hidden)"
            )
            print(f"Next: {sys.executable} room_agent/setup_tv_connector.py doctor")
            print(f"Then: {sys.executable} room_agent/setup_tv_connector.py flash")
            return 0

        if args.command == "doctor":
            checks = inspect_configuration(
                key_file=args.key_file,
                secrets_file=args.secrets_file,
            )
            for passed, message in checks:
                print(f"[{'ok' if passed else 'fix'}] {message}")
            if all(passed for passed, _message in checks):
                print("Ready to build and flash.")
                return 0
            print("Setup is incomplete. Run prepare, then install PlatformIO if requested.")
            return 1

        if args.command == "connect-host":
            url, profile = connect_host(
                url=args.url,
                timeout=args.timeout,
                key_file=args.key_file,
                secrets_file=args.secrets_file,
                env_file=args.env_file,
            )
            print("TV connector verified and saved for the voice host.")
            print(f"  URL: {url}")
            print(f"  Profile: {profile}")
            print("Next: install the Windows voice host and pair the iPhone Shortcut.")
            return 0

        return run_platformio(
            emitter=args.emitter,
            upload=args.command == "flash",
            port=getattr(args, "port", None),
        )
    except (OSError, UnicodeError, ProtocolError, TvConnectorError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

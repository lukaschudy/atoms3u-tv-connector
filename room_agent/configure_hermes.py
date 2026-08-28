"""Safely wire the restricted Hermes sidecar into an existing Pi install."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
from pathlib import Path


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class HermesConfigurationError(RuntimeError):
    pass


def read_env_values(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise HermesConfigurationError(f"Cannot read {path}: {exc}") from exc
    result = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if _ENV_KEY.fullmatch(key):
            result[key] = value.strip()
    return result


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    for key, value in updates.items():
        if not _ENV_KEY.fullmatch(key) or "\n" in value or "\r" in value:
            raise HermesConfigurationError("Invalid environment update.")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise HermesConfigurationError(f"Cannot read {path}: {exc}") from exc

    remaining = dict(updates)
    output = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    if remaining and output and output[-1]:
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    try:
        temp_path.write_text("\n".join(output) + "\n", encoding="utf-8")
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
        path.chmod(0o600)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HermesConfigurationError(f"Cannot update {path}: {exc}") from exc


def configure_hermes(
    *,
    home: Path,
    room_dir: Path,
    secret_factory=secrets.token_hex,
) -> dict[str, Path]:
    source_dir = Path(__file__).resolve().parent
    room_env = room_dir / ".env"
    room_values = read_env_values(room_env)
    openrouter_key = room_values.get("OPENROUTER_API_KEY", "").strip()
    if not openrouter_key or any(character.isspace() for character in openrouter_key):
        raise HermesConfigurationError(
            "The room-agent .env must contain a valid OPENROUTER_API_KEY first."
        )

    hermes_home = home / ".hermes"
    hermes_env = hermes_home / ".env"
    hermes_values = read_env_values(hermes_env)
    server_key = hermes_values.get("API_SERVER_KEY", "").strip()
    client_key = room_values.get("HERMES_API_KEY", "").strip()
    if server_key and client_key and server_key != client_key:
        raise HermesConfigurationError(
            "Existing Hermes server and room-agent client keys do not match."
        )
    sidecar_key = server_key or client_key or secret_factory(32)
    if len(sidecar_key) < 16 or any(character.isspace() for character in sidecar_key):
        raise HermesConfigurationError("The existing Hermes API key is too weak.")

    update_env_file(
        hermes_env,
        {
            "OPENROUTER_API_KEY": openrouter_key,
            "API_SERVER_ENABLED": "true",
            "API_SERVER_HOST": "127.0.0.1",
            "API_SERVER_PORT": "8642",
            "API_SERVER_KEY": sidecar_key,
        },
    )
    update_env_file(
        room_env,
        {
            "HERMES_API_URL": "http://127.0.0.1:8642",
            "HERMES_API_KEY": sidecar_key,
            "HERMES_API_MODEL": "hermes-agent",
            "HERMES_TIMEOUT_SECONDS": "25.0",
        },
    )

    hermes_home.mkdir(parents=True, exist_ok=True)
    config_path = hermes_home / "config.yaml"
    config_source = source_dir / "hermes-config.example.yaml"
    if config_path.exists() and config_path.read_bytes() != config_source.read_bytes():
        backup = hermes_home / "config.before-room-agent.yaml"
        if not backup.exists():
            shutil.copy2(config_path, backup)
    shutil.copy2(config_source, config_path)

    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "room-agent-hermes.service"
    shutil.copy2(source_dir / "room-agent-hermes.service", unit_path)
    room_unit_path = unit_dir / "room-agent.service"
    shutil.copy2(source_dir / "room-agent.service", room_unit_path)
    return {
        "hermes_env": hermes_env,
        "room_env": room_env,
        "config": config_path,
        "unit": unit_path,
        "room_unit": room_unit_path,
    }


def main() -> int:
    home = Path.home()
    room_dir = Path(__file__).resolve().parent
    hermes_binary = home / ".local" / "bin" / "hermes"
    if not hermes_binary.is_file():
        print(
            "ERROR: Hermes is not installed. Run the official installer first.",
            file=sys.stderr,
        )
        return 2
    try:
        paths = configure_hermes(home=home, room_dir=room_dir)
    except HermesConfigurationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("Hermes sidecar configuration is ready; no secret values were printed.")
    print(f"Config: {paths['config']}")
    print(f"Hermes service: {paths['unit']}")
    print(f"Room service: {paths['room_unit']}")
    print("Next: systemctl --user daemon-reload")
    print("Then: systemctl --user enable --now room-agent-hermes.service")
    print("Then: systemctl --user restart room-agent.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Small authenticated protocol shared by the Pi hub and device companions."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping


PROTOCOL_VERSION = 1
MAX_BODY_BYTES = 8_192
MAX_CLOCK_SKEW_SECONDS = 30
NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
RESPONSE_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RESPONSE_DOMAIN = b"room-agent-response-v1\n"
_WINDOWS_PRIVATE_PATH_ENV = "ROOM_AGENT_PRIVATE_PATH"
_MAX_POWERSHELL_EXCEPTION_LENGTH = 160
_POWERSHELL_EXCEPTION_PATTERN = re.compile(
    r"^System(?:\.[A-Za-z_][A-Za-z0-9_]*)+Exception$"
)


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class ActionRequest:
    action: str
    target: str
    arguments: dict[str, Any]
    request_id: str
    version: int = PROTOCOL_VERSION

    @classmethod
    def create(cls, action: str, target: str, arguments: dict[str, Any]) -> "ActionRequest":
        return cls(
            action=action,
            target=target,
            arguments=arguments,
            request_id=str(uuid.uuid4()),
        )

    def to_bytes(self) -> bytes:
        payload = {
            "version": self.version,
            "request_id": self.request_id,
            "action": self.action,
            "target": self.target,
            "arguments": self.arguments,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

    @classmethod
    def from_bytes(cls, body: bytes) -> "ActionRequest":
        if not body or len(body) > MAX_BODY_BYTES:
            raise ProtocolError("Invalid request-body size.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("Request body is not valid UTF-8 JSON.") from exc
        if not isinstance(payload, dict):
            raise ProtocolError("Request body must be a JSON object.")

        required = {"version", "request_id", "action", "target", "arguments"}
        if set(payload) != required:
            raise ProtocolError("Request fields do not match the protocol schema.")
        if payload["version"] != PROTOCOL_VERSION:
            raise ProtocolError("Unsupported protocol version.")
        try:
            uuid.UUID(str(payload["request_id"]))
        except ValueError as exc:
            raise ProtocolError("Invalid request_id.") from exc
        if not isinstance(payload["action"], str) or not isinstance(payload["target"], str):
            raise ProtocolError("Action and target must be strings.")
        if not isinstance(payload["arguments"], dict):
            raise ProtocolError("Arguments must be an object.")
        return cls(
            version=payload["version"],
            request_id=str(payload["request_id"]),
            action=payload["action"],
            target=payload["target"],
            arguments=payload["arguments"],
        )


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _open_checked_private_descriptor(path: Path, *, require_private: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if os.name == "posix":
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("symlink-safe private-file reads are unsupported")
        flags |= no_follow
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        by_path = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(by_path.st_mode)
            or not _same_file_identity(opened, by_path)
        ):
            raise OSError("private path must be one stable, regular, non-symlink file")
        if require_private:
            if os.name == "nt":
                _verify_windows_private_acl(path)
                after_acl = path.stat(follow_symlinks=False)
                if not _same_file_identity(opened, after_acl):
                    raise OSError("private file changed while its ACL was verified")
            elif os.name == "posix":
                mode = stat.S_IMODE(opened.st_mode)
                if mode & 0o077:
                    raise OSError(f"private file mode is {mode:o}, expected 600")
            else:
                raise OSError("private-file permissions are unsupported on this platform")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_private_permissions(path: Path) -> None:
    descriptor = _open_checked_private_descriptor(path, require_private=True)
    os.close(descriptor)


def _apply_windows_private_acl(path: Path) -> None:
    """Replace the file ACL with one full-control rule for the current user."""

    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    hidden = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    script = (
        "& { try { "
        "$ErrorActionPreference='Stop';"
        "$TargetPath=[Environment]::GetEnvironmentVariable("
        "'ROOM_AGENT_PRIVATE_PATH');"
        "if([string]::IsNullOrEmpty($TargetPath)){throw 'missing private path'};"
        "$current=[Security.Principal.WindowsIdentity]::GetCurrent().User;"
        "$acl=New-Object Security.AccessControl.FileSecurity;"
        "$acl.SetOwner($current);"
        "$acl.SetAccessRuleProtection($true,$false);"
        "$rule=New-Object Security.AccessControl.FileSystemAccessRule("
        "$current,[Security.AccessControl.FileSystemRights]::FullControl,"
        "[Security.AccessControl.AccessControlType]::Allow);"
        "$acl.AddAccessRule($rule);"
        "[System.IO.File]::SetAccessControl($TargetPath,$acl);"
        "} catch { [Console]::Out.Write("
        "$_.Exception.GetType().FullName); exit 5 } }"
    )
    try:
        restricted = subprocess.run(
            [
                str(system_root / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=hidden,
            env={**os.environ, _WINDOWS_PRIVATE_PATH_ENV: str(path)},
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise OSError(f"could not enforce an owner-only Windows ACL: {exc}") from exc
    if restricted.returncode != 0:
        diagnostic = restricted.stdout.strip()
        if (
            len(diagnostic) <= _MAX_POWERSHELL_EXCEPTION_LENGTH
            and _POWERSHELL_EXCEPTION_PATTERN.fullmatch(diagnostic)
        ):
            raise OSError(
                f"could not apply an owner-only Windows ACL ({diagnostic})"
            )
        raise OSError("could not apply an owner-only Windows ACL")


def _verify_windows_private_acl(path: Path) -> None:
    """Require one explicit allow rule for only the current Windows user."""

    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    hidden = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    script = (
        "& { "
        "$ErrorActionPreference='Stop';"
        "$TargetPath=[Environment]::GetEnvironmentVariable("
        "'ROOM_AGENT_PRIVATE_PATH');"
        "if([string]::IsNullOrEmpty($TargetPath)){throw 'missing private path'};"
        "$acl=[System.IO.File]::GetAccessControl($TargetPath);"
        "$current=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
        "$owner=$acl.GetOwner("
        "[Security.Principal.SecurityIdentifier]).Value;"
        "$rules=@($acl.Access);"
        "if($owner -ne $current -or -not $acl.AreAccessRulesProtected -or "
        "$rules.Count -ne 1){exit 3};"
        "$rule=$rules[0];"
        "$sid=$rule.IdentityReference.Translate("
        "[Security.Principal.SecurityIdentifier]).Value;"
        "$full=[Security.AccessControl.FileSystemRights]::FullControl;"
        "if($sid -ne $current -or $rule.IsInherited -or "
        "$rule.AccessControlType -ne "
        "[Security.AccessControl.AccessControlType]::Allow -or "
        "$rule.FileSystemRights -ne $full){exit 4} }"
    )
    try:
        verified = subprocess.run(
            [
                str(system_root / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=hidden,
            env={**os.environ, _WINDOWS_PRIVATE_PATH_ENV: str(path)},
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise OSError(f"could not verify the owner-only Windows ACL: {exc}") from exc
    if verified.returncode != 0:
        raise OSError("private file does not have an owner-only Windows ACL")


def _restrict_private_permissions(path: Path, descriptor: int) -> None:
    if os.name == "nt":
        _apply_windows_private_acl(path)
    else:
        os.fchmod(descriptor, 0o600)


def write_private_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    replace: bool = True,
    description: str = "private file",
) -> None:
    """Write secret-bearing text without a permissive or symlink-following window."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: Path | None = None
    destination_created = False
    committed = False
    try:
        if replace:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary = Path(temporary_name)
            destination = temporary
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            destination = path
            destination_created = True

        _restrict_private_permissions(destination, descriptor)
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _verify_private_permissions(destination)
        if temporary is not None:
            os.replace(temporary, path)
            temporary = None
        committed = True
    except FileExistsError as exc:
        raise ProtocolError(f"Refusing to overwrite existing {description}: {path}") from exc
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        raise ProtocolError(
            f"Could not enforce private permissions for {description}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if destination_created and not committed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def generate_key(path: Path) -> None:
    write_private_text(
        path,
        secrets.token_hex(32),
        encoding="ascii",
        replace=False,
        description="connector key",
    )


def verify_private_file(path: Path, *, description: str) -> None:
    try:
        _verify_private_permissions(path)
    except OSError as exc:
        raise ProtocolError(f"{description} permissions are unsafe: {exc}") from exc


def read_private_text(
    path: Path,
    *,
    encoding: str = "utf-8",
    require_private: bool = True,
    description: str = "private file",
    max_bytes: int = 65_536,
) -> str:
    """Read one stable, non-symlink regular file through its verified descriptor."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    descriptor = -1
    try:
        descriptor = _open_checked_private_descriptor(
            path,
            require_private=require_private,
        )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except (OSError, UnicodeError) as exc:
        raise ProtocolError(f"Cannot read {description} {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > max_bytes:
        raise ProtocolError(f"{description.capitalize()} is unexpectedly large.")
    try:
        return payload.decode(encoding)
    except UnicodeError as exc:
        raise ProtocolError(f"Cannot decode {description} {path}: {exc}") from exc


def read_key(path: Path, *, require_private: bool = True) -> bytes:
    try:
        raw_value = read_private_text(
            path,
            require_private=require_private,
            encoding="ascii",
            description="pairing key",
            max_bytes=128,
        )
    except ProtocolError as exc:
        raise ProtocolError(f"Cannot read pairing key {path}: {exc}") from exc
    value = raw_value.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ProtocolError("Pairing key must contain exactly 64 hexadecimal characters.")
    return bytes.fromhex(value)


def _signature(secret: bytes, timestamp: str, nonce: str, body: bytes) -> str:
    message = timestamp.encode("ascii") + b"\n" + nonce.encode("ascii") + b"\n" + body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def signed_headers(secret: bytes, body: bytes, now: int | None = None) -> dict[str, str]:
    timestamp = str(int(time.time() if now is None else now))
    nonce = secrets.token_hex(16)
    return {
        "Content-Type": "application/json",
        "X-Room-Agent-Timestamp": timestamp,
        "X-Room-Agent-Nonce": nonce,
        "X-Room-Agent-Signature": _signature(secret, timestamp, nonce, body),
    }


def sign_response(secret: bytes, body: bytes) -> str:
    """Authenticate an exact response body in a response-specific domain."""

    if len(secret) != 32:
        raise ProtocolError("Pairing key must contain exactly 32 bytes.")
    return hmac.new(secret, _RESPONSE_DOMAIN + body, hashlib.sha256).hexdigest()


def verify_response(secret: bytes, body: bytes, supplied: str) -> None:
    """Reject unsigned or modified response bodies."""

    expected = sign_response(secret, body)
    if not RESPONSE_SIGNATURE_PATTERN.fullmatch(supplied) or not hmac.compare_digest(
        expected, supplied
    ):
        raise ProtocolError("The connector response signature is invalid.")


def verify_request_authentication(
    secret: bytes,
    body: bytes,
    headers: Mapping[str, str],
    now: int | None = None,
) -> tuple[str, int, int]:
    current = int(time.time() if now is None else now)
    timestamp_text = headers.get("X-Room-Agent-Timestamp", "")
    nonce = headers.get("X-Room-Agent-Nonce", "")
    supplied = headers.get("X-Room-Agent-Signature", "")

    try:
        timestamp = int(timestamp_text)
    except ValueError as exc:
        raise ProtocolError("Invalid request timestamp.") from exc
    if abs(current - timestamp) > MAX_CLOCK_SKEW_SECONDS:
        raise ProtocolError("Request timestamp is outside the allowed window.")
    if not NONCE_PATTERN.fullmatch(nonce):
        raise ProtocolError("Invalid request nonce.")

    expected = _signature(secret, timestamp_text, nonce, body)
    if not re.fullmatch(r"[0-9a-f]{64}", supplied) or not hmac.compare_digest(
        expected, supplied
    ):
        raise ProtocolError("Request signature is invalid.")
    return nonce, timestamp + MAX_CLOCK_SKEW_SECONDS, current


def consume_verified_nonce(
    nonce: str,
    expiry: int,
    seen_nonces: MutableMapping[str, int],
    *,
    now: int,
) -> None:
    expired = [key for key, stored_expiry in seen_nonces.items() if stored_expiry < now]
    for key in expired:
        seen_nonces.pop(key, None)
    if nonce in seen_nonces:
        raise ProtocolError("Request nonce has already been used.")
    seen_nonces[nonce] = expiry


def verify_headers(
    secret: bytes,
    body: bytes,
    headers: Mapping[str, str],
    seen_nonces: MutableMapping[str, int],
    now: int | None = None,
) -> None:
    nonce, expiry, current = verify_request_authentication(
        secret,
        body,
        headers,
        now=now,
    )
    consume_verified_nonce(nonce, expiry, seen_nonces, now=current)

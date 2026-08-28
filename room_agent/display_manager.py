"""Capability-aware native TV selection with a local HDMI fallback.

Only explicitly enrolled endpoints are considered. Discovery proves reachability and
capabilities; it never grants control of an arbitrary device found on the LAN.
"""

from __future__ import annotations

import json
import ipaddress
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


CONFIG_VERSION = 1
MAX_CONFIG_BYTES = 65_536
MAX_ENDPOINTS = 16
MAX_WEB_QUERY_CHARACTERS = 120

CAP_APP_YOUTUBE = "app.youtube"
CAP_APP_NETFLIX = "app.netflix"
CAP_WEB_BROWSER = "web.browser"
CAP_WEB_SEARCH = "web.search"
CAP_WEB_OPEN_FIRST = "web.open_first"
CAP_YOUTUBE_PLAY = "youtube.play"
CAP_MEDIA_PAUSE = "media.pause"
CAP_MEDIA_RESUME = "media.resume"
CAP_MEDIA_CLOSE = "media.close"

HDMI_CAPABILITIES = frozenset(
    {
        CAP_APP_YOUTUBE,
        CAP_WEB_BROWSER,
        CAP_WEB_SEARCH,
        CAP_WEB_OPEN_FIRST,
        CAP_YOUTUBE_PLAY,
        CAP_MEDIA_PAUSE,
        CAP_MEDIA_RESUME,
        CAP_MEDIA_CLOSE,
    }
)

_ENDPOINT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ADB_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_APPLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9:._-]{3,160}$")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_APP_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_APPLE_APP = re.compile(r"App:\s*(?P<name>.*?)\s*\((?P<identifier>[^()]+)\)")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?)+$"
)


class DisplayError(RuntimeError):
    """Base error for display configuration, probing and execution."""


class DisplayConfigError(DisplayError):
    pass


class DisplayControlError(DisplayError):
    pass


def app_capability(app: str) -> str:
    if not _APP_SLUG.fullmatch(app):
        raise DisplayControlError("The native app name is invalid.")
    return f"app.{app}"


def _app_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    return slug[:48]


@dataclass(frozen=True)
class EndpointStatus:
    endpoint_id: str
    name: str
    kind: str
    online: bool
    capabilities: frozenset[str]
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.endpoint_id,
            "name": self.name,
            "kind": self.kind,
            "online": self.online,
            "capabilities": sorted(self.capabilities),
            "detail": self.detail,
        }


class DisplayEndpoint:
    """Small, typed interface implemented by each trusted display backend."""

    kind = "unknown"

    def __init__(self, endpoint_id: str, name: str, priority: int) -> None:
        if not _ENDPOINT_ID.fullmatch(endpoint_id):
            raise DisplayConfigError(f"Invalid display endpoint id: {endpoint_id!r}")
        if not name.strip() or len(name) > 80:
            raise DisplayConfigError("Display endpoint names must contain 1-80 characters.")
        if not 0 <= priority <= 1_000:
            raise DisplayConfigError("Display endpoint priority must be between 0 and 1000.")
        self.endpoint_id = endpoint_id
        self.name = name.strip()
        self.priority = priority

    def probe(self) -> EndpointStatus:
        raise NotImplementedError

    def open_site(self, site: str) -> dict[str, str]:
        raise DisplayControlError(f"{self.name} cannot open websites.")

    def launch_app(self, app: str) -> dict[str, str]:
        raise DisplayControlError(f"{self.name} cannot launch {app}.")

    def search_google(self, query: str) -> dict[str, str]:
        raise DisplayControlError(f"{self.name} has no verified web browser.")

    def open_first_google_result(self) -> dict[str, str]:
        raise DisplayControlError(f"{self.name} cannot continue the web search.")

    def play_youtube(self, video_id: str) -> dict[str, str]:
        raise DisplayControlError(f"{self.name} cannot play a YouTube link.")

    def pause(self) -> dict[str, str]:
        raise DisplayControlError(f"{self.name} cannot pause media.")

    def resume(self) -> dict[str, str]:
        raise DisplayControlError(f"{self.name} cannot resume media.")

    def close(self) -> dict[str, str]:
        raise DisplayControlError(f"{self.name} cannot return home.")


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME.fullmatch(host))


class HdmiDisplayEndpoint(DisplayEndpoint):
    kind = "pi_hdmi"

    def __init__(self, player: Any) -> None:
        super().__init__("pi_hdmi", "Raspberry Pi HDMI", 0)
        self.player = player

    def probe(self) -> EndpointStatus:
        available = getattr(self.player, "is_available", None)
        try:
            online, detail = available() if callable(available) else (True, "HDMI fallback ready")
        except Exception as exc:  # The player boundary must never break discovery.
            online, detail = False, f"HDMI probe failed: {exc}"
        return EndpointStatus(
            self.endpoint_id,
            self.name,
            self.kind,
            bool(online),
            HDMI_CAPABILITIES if online else frozenset(),
            str(detail),
        )

    def _call(self, method: str, *arguments: str) -> dict[str, str]:
        try:
            return getattr(self.player, method)(*arguments)
        except Exception as exc:
            raise DisplayControlError(str(exc)) from exc

    def open_site(self, site: str) -> dict[str, str]:
        return self._call("open_site", site)

    def launch_app(self, app: str) -> dict[str, str]:
        if app != "youtube":
            raise DisplayControlError(f"The HDMI fallback does not allowlist {app}.")
        return self.open_site("youtube")

    def search_google(self, query: str) -> dict[str, str]:
        return self._call("search_google", query)

    def open_first_google_result(self) -> dict[str, str]:
        return self._call("open_first_google_result")

    def play_youtube(self, video_id: str) -> dict[str, str]:
        return self._call("play_youtube", video_id)

    def pause(self) -> dict[str, str]:
        return self._call("pause")

    def resume(self) -> dict[str, str]:
        return self._call("resume")

    def close(self) -> dict[str, str]:
        result = self._call("close")
        home_url = getattr(self.player, "home_url", None)
        if home_url:
            result = dict(result)
            result["home"] = self._call("show_home")["opened"]
        return result


Runner = Callable[..., subprocess.CompletedProcess[str]]


class CommandEndpoint(DisplayEndpoint):
    def __init__(
        self,
        endpoint_id: str,
        name: str,
        priority: int,
        *,
        runner: Runner = subprocess.run,
        timeout: float = 8.0,
    ) -> None:
        super().__init__(endpoint_id, name, priority)
        self.runner = runner
        self.timeout = timeout

    def _run(self, command: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                list(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
            )
        except FileNotFoundError as exc:
            raise DisplayControlError(f"Control executable not found: {command[0]}") from exc
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise DisplayControlError(f"Control command failed: {exc}") from exc

    @staticmethod
    def _detail(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stderr or result.stdout or f"exit code {result.returncode}").strip()


class AndroidTvEndpoint(CommandEndpoint):
    """Paired Google/Android TV controlled through a verified ADB session."""

    kind = "android_tv"
    DEFAULT_APP_PACKAGES = {
        "youtube": "com.google.android.youtube.tv",
        "netflix": "com.netflix.ninja",
    }

    def __init__(
        self,
        endpoint_id: str,
        name: str,
        priority: int,
        *,
        serial: str | None = None,
        host: str | None = None,
        adb: str = "adb",
        apps: Mapping[str, str] | None = None,
        connect_on_probe: bool = True,
        runner: Runner = subprocess.run,
        media_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(endpoint_id, name, priority, runner=runner)
        serial = serial.strip() if serial else None
        host = host.strip() if host else None
        if serial and not _ADB_SERIAL.fullmatch(serial):
            raise DisplayConfigError("The Android TV ADB serial is invalid.")
        if host and not _valid_host(host):
            raise DisplayConfigError("The Android TV host is invalid.")
        if not serial and not host:
            raise DisplayConfigError("Android TV requires a paired serial or host.")
        self.configured_serial = serial
        self.host = host or (serial.rsplit(":", 1)[0] if serial and ":" in serial else serial)
        self.adb = adb
        configured_apps = self.DEFAULT_APP_PACKAGES if apps is None else apps
        self.app_packages = {}
        for app, package in configured_apps.items():
            app = _app_slug(app)
            if not _APP_SLUG.fullmatch(app) or not re.fullmatch(
                r"[A-Za-z0-9_.]{3,200}", package
            ):
                raise DisplayConfigError("An Android TV app registry entry is invalid.")
            self.app_packages[app] = package
        self.connect_on_probe = connect_on_probe
        self.media_timeout = media_timeout
        self.clock = clock
        self.sleeper = sleeper
        self._active_serial: str | None = None
        self._capabilities: frozenset[str] = frozenset()
        self._last_google_query: str | None = None
        self._browser_component: str | None = None

    def _adb(self, *arguments: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        return self._run([self.adb, *arguments], timeout=timeout)

    def _devices(self) -> dict[str, str]:
        result = self._adb("devices", "-l")
        if result.returncode != 0:
            raise DisplayControlError(f"ADB device discovery failed: {self._detail(result)}")
        devices: dict[str, str] = {}
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2:
                devices[fields[0]] = fields[1]
        return devices

    def _mdns_connect_addresses(self) -> list[str]:
        if self.host is None:
            return []
        result = self._adb("mdns", "services")
        if result.returncode != 0:
            return []
        addresses = []
        for line in result.stdout.splitlines():
            if "_adb-tls-connect._tcp" not in line:
                continue
            fields = line.split()
            if not fields:
                continue
            address = fields[-1]
            if _ADB_SERIAL.fullmatch(address) and address.startswith(f"{self.host}:"):
                addresses.append(address)
        return addresses

    def _resolve_serial(self) -> str:
        devices = self._devices()
        candidates = [
            serial
            for serial, state in devices.items()
            if state == "device"
            and (
                serial == self.configured_serial
                or (self.host is not None and serial.startswith(f"{self.host}:"))
            )
        ]
        if not candidates and self.connect_on_probe and self.configured_serial:
            self._adb("connect", self.configured_serial, timeout=6.0)
            devices = self._devices()
            candidates = [
                serial
                for serial, state in devices.items()
                if state == "device"
                and (
                    serial == self.configured_serial
                    or (self.host is not None and serial.startswith(f"{self.host}:"))
                )
            ]
        if not candidates and self.connect_on_probe:
            for address in self._mdns_connect_addresses():
                self._adb("connect", address, timeout=6.0)
            devices = self._devices()
            candidates = [
                serial
                for serial, state in devices.items()
                if state == "device"
                and self.host is not None
                and serial.startswith(f"{self.host}:")
            ]
        if len(candidates) != 1:
            if not candidates:
                raise DisplayControlError("paired Android TV is not connected over ADB")
            raise DisplayControlError("multiple paired ADB endpoints match this Android TV")
        self._active_serial = candidates[0]
        return candidates[0]

    def _shell(self, *arguments: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        serial = self._active_serial or self._resolve_serial()
        return self._adb("-s", serial, "shell", *arguments, timeout=timeout)

    def _package_installed(self, package: str) -> bool:
        result = self._shell("pm", "path", package)
        return result.returncode == 0 and "package:" in result.stdout

    def _browser_available(self) -> bool:
        result = self._shell(
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-a",
            "android.intent.action.VIEW",
            "-c",
            "android.intent.category.BROWSABLE",
            "-d",
            "https://www.google.com/search?q=room-agent",
        )
        output = f"{result.stdout}\n{result.stderr}".casefold()
        component = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        available = (
            result.returncode == 0
            and "no activity" not in output
            and "resolveractivity" not in output
            and re.fullmatch(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+", component) is not None
        )
        self._browser_component = component if available else None
        return available

    def probe(self) -> EndpointStatus:
        try:
            serial = self._resolve_serial()
            model_result = self._shell("getprop", "ro.product.model")
            model = model_result.stdout.strip() or self.name
            capabilities = {CAP_MEDIA_PAUSE, CAP_MEDIA_RESUME, CAP_MEDIA_CLOSE}
            for app, package in self.app_packages.items():
                if self._package_installed(package):
                    capabilities.add(app_capability(app))
                    if app == "youtube":
                        capabilities.add(CAP_YOUTUBE_PLAY)
            if self._browser_available():
                capabilities.update(
                    {CAP_WEB_BROWSER, CAP_WEB_SEARCH, CAP_WEB_OPEN_FIRST}
                )
            self._capabilities = frozenset(capabilities)
            return EndpointStatus(
                self.endpoint_id,
                self.name,
                self.kind,
                True,
                self._capabilities,
                f"{model} connected as {serial}",
            )
        except DisplayControlError as exc:
            self._active_serial = None
            self._capabilities = frozenset()
            self._browser_component = None
            return EndpointStatus(
                self.endpoint_id,
                self.name,
                self.kind,
                False,
                frozenset(),
                str(exc),
            )

    def _require_success(self, result: subprocess.CompletedProcess[str], operation: str) -> None:
        output = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0 or re.search(r"(?:error|unable to resolve)", output, re.I):
            raise DisplayControlError(f"Android TV {operation} failed: {self._detail(result)}")

    def _wait_foreground(self, package: str) -> None:
        deadline = self.clock() + self.media_timeout
        last_detail = "foreground application was not exposed"
        while True:
            result = self._shell("dumpsys", "window", "windows")
            output = f"{result.stdout}\n{result.stderr}"
            if result.returncode == 0 and package in output:
                return
            last_detail = self._detail(result)
            if self.clock() >= deadline:
                raise DisplayControlError(
                    f"Android TV did not bring {package} to the foreground "
                    f"({last_detail})."
                )
            self.sleeper(0.25)

    def _wait_media_state(self, expected_state: int, status: str) -> dict[str, str]:
        deadline = self.clock() + self.media_timeout
        last_state = "not exposed"
        while True:
            state_result = self._shell("dumpsys", "media_session")
            states = re.findall(r"\bstate=(\d+)\b", state_result.stdout)
            last_state = ",".join(states) if states else "not exposed"
            if str(expected_state) in states:
                return {"media_status": status, "player": "android-tv-adb"}
            if self.clock() >= deadline:
                raise DisplayControlError(
                    f"Android TV did not confirm {status.casefold()} "
                    f"(media states: {last_state})."
                )
            self.sleeper(0.25)

    def launch_app(self, app: str) -> dict[str, str]:
        package = self.app_packages.get(app)
        if package is None or app_capability(app) not in self._capabilities:
            raise DisplayControlError(f"{app} is not installed on the Android TV.")
        result = self._shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        self._require_success(result, f"launch of {app}")
        self._wait_foreground(package)
        self._last_google_query = None
        return {"app": app, "package": package, "player": "android-tv-adb"}

    def open_site(self, site: str) -> dict[str, str]:
        if site == "youtube":
            return self.launch_app("youtube")
        if site != "miton" or CAP_WEB_BROWSER not in self._capabilities:
            raise DisplayControlError("The Android TV has no verified browser for this site.")
        return self._open_url(
            "https://www.miton.cz/", component=self._browser_component
        )

    def _open_url(
        self,
        url: str,
        package: str | None = None,
        *,
        component: str | None = None,
        autoplay: bool = False,
    ) -> dict[str, str]:
        command = [
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.VIEW",
            "-c",
            "android.intent.category.BROWSABLE",
        ]
        if autoplay:
            command.extend(["--ez", "android.intent.extra.START_PLAYBACK", "true"])
        command.extend(["-d", url])
        if package:
            command.append(package)
        elif component:
            command.extend(["-n", component])
        result = self._shell(*command, timeout=12.0)
        self._require_success(result, "deep link")
        expected_package = package or (component.split("/", 1)[0] if component else None)
        if expected_package:
            self._wait_foreground(expected_package)
        return {"opened": url, "player": "android-tv-adb"}

    def search_google(self, query: str) -> dict[str, str]:
        query = " ".join(query.split())
        if CAP_WEB_SEARCH not in self._capabilities or not query or len(query) > MAX_WEB_QUERY_CHARACTERS:
            raise DisplayControlError("The Android TV has no verified browser for this search.")
        url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
        result = self._open_url(url, component=self._browser_component)
        self._last_google_query = query
        return result

    def open_first_google_result(self) -> dict[str, str]:
        if CAP_WEB_OPEN_FIRST not in self._capabilities or self._last_google_query is None:
            raise DisplayControlError("There is no native Android TV search to continue.")
        url = "https://www.google.com/search?" + urllib.parse.urlencode(
            {"q": self._last_google_query, "btnI": "1"}
        )
        return self._open_url(url, component=self._browser_component)

    def play_youtube(self, video_id: str) -> dict[str, str]:
        if CAP_YOUTUBE_PLAY not in self._capabilities or not _VIDEO_ID.fullmatch(video_id):
            raise DisplayControlError("Native YouTube playback is unavailable.")
        url = f"https://www.youtube.com/watch?v={video_id}"
        result = self._open_url(
            url,
            self.app_packages["youtube"],
            autoplay=True,
        )
        result.update(self._wait_media_state(3, "Playing"))
        self._last_google_query = None
        return result

    def _media_key(self, key: str, status: str, expected_state: int) -> dict[str, str]:
        result = self._shell("input", "keyevent", key)
        self._require_success(result, status.casefold())
        return self._wait_media_state(expected_state, status)

    def pause(self) -> dict[str, str]:
        return self._media_key("KEYCODE_MEDIA_PAUSE", "Paused", 2)

    def resume(self) -> dict[str, str]:
        return self._media_key("KEYCODE_MEDIA_PLAY", "Playing", 3)

    def close(self) -> dict[str, str]:
        result = self._shell("input", "keyevent", "KEYCODE_HOME")
        self._require_success(result, "home")
        self._last_google_query = None
        return {"media_status": "Home", "player": "android-tv-adb"}


class AppleTvEndpoint(CommandEndpoint):
    """Paired Apple TV controlled through pyatv's atvremote companion."""

    kind = "apple_tv"

    def __init__(
        self,
        endpoint_id: str,
        name: str,
        priority: int,
        *,
        host: str,
        identifier: str,
        atvremote: str = "atvremote",
        runner: Runner = subprocess.run,
    ) -> None:
        super().__init__(endpoint_id, name, priority, runner=runner, timeout=12.0)
        if not host.strip() or not _valid_host(host.strip()):
            raise DisplayConfigError("Apple TV host is invalid.")
        if not _APPLE_IDENTIFIER.fullmatch(identifier):
            raise DisplayConfigError("Apple TV identifier is invalid.")
        self.host = host.strip()
        self.identifier = identifier
        self.atvremote = str(Path(atvremote).expanduser()) if "/" in atvremote else atvremote
        self._apps: dict[str, str] = {}

    def _atv(self, *commands: str) -> subprocess.CompletedProcess[str]:
        return self._run(
            [
                self.atvremote,
                "-s",
                self.host,
                "-i",
                self.identifier,
                "--protocol",
                "companion",
                *commands,
            ]
        )

    @staticmethod
    def _parse_apps(output: str) -> dict[str, str]:
        return {
            _app_slug(match.group("name")): match.group("identifier").strip()
            for match in _APPLE_APP.finditer(output)
            if _APP_SLUG.fullmatch(_app_slug(match.group("name")))
        }

    def _find_app(self, app: str) -> str | None:
        return self._apps.get(_app_slug(app))

    def probe(self) -> EndpointStatus:
        try:
            result = self._atv("app_list")
        except DisplayControlError as exc:
            return EndpointStatus(
                self.endpoint_id, self.name, self.kind, False, frozenset(), str(exc)
            )
        if result.returncode != 0:
            return EndpointStatus(
                self.endpoint_id,
                self.name,
                self.kind,
                False,
                frozenset(),
                f"Apple TV pairing/probe failed: {self._detail(result)}",
            )
        self._apps = self._parse_apps(result.stdout)
        capabilities = {CAP_MEDIA_PAUSE, CAP_MEDIA_RESUME, CAP_MEDIA_CLOSE}
        capabilities.update(app_capability(app) for app in self._apps)
        return EndpointStatus(
            self.endpoint_id,
            self.name,
            self.kind,
            True,
            frozenset(capabilities),
            f"paired; {len(self._apps)} launchable apps",
        )

    def _require_success(self, result: subprocess.CompletedProcess[str], operation: str) -> None:
        if result.returncode != 0:
            raise DisplayControlError(f"Apple TV {operation} failed: {self._detail(result)}")

    def launch_app(self, app: str) -> dict[str, str]:
        app = _app_slug(app)
        identifier = self._find_app(app)
        if identifier is None:
            raise DisplayControlError(f"{app} is not installed on the Apple TV.")
        result = self._atv(f"launch_app={identifier}")
        self._require_success(result, f"launch of {app}")
        return {"app": app, "bundle_id": identifier, "player": "apple-tv-pyatv"}

    def open_site(self, site: str) -> dict[str, str]:
        if site != "youtube":
            raise DisplayControlError("Apple TV has no general web browser.")
        return self.launch_app("youtube")

    def play_youtube(self, video_id: str) -> dict[str, str]:
        if self._find_app("youtube") is None or not _VIDEO_ID.fullmatch(video_id):
            raise DisplayControlError("Native YouTube playback is unavailable on Apple TV.")
        url = f"https://www.youtube.com/watch?v={video_id}"
        result = self._atv(f"launch_app={url}")
        self._require_success(result, "YouTube deep link")
        return {"opened": url, "player": "apple-tv-pyatv"}

    def _remote(self, command: str, status: str) -> dict[str, str]:
        result = self._atv(command)
        self._require_success(result, command)
        return {"media_status": status, "player": "apple-tv-pyatv"}

    def pause(self) -> dict[str, str]:
        return self._remote("pause", "PauseRequested")

    def resume(self) -> dict[str, str]:
        return self._remote("play", "PlayRequested")

    def close(self) -> dict[str, str]:
        return self._remote("home", "Home")


class LgWebOsEndpoint(CommandEndpoint):
    """Paired LG webOS TV controlled through a restricted helper process."""

    kind = "lg_webos"

    def __init__(
        self,
        endpoint_id: str,
        name: str,
        priority: int,
        *,
        host: str,
        key_file: str,
        python: str = "/home/voiceagent/room-agent/.venv/bin/python",
        helper: str = "/home/voiceagent/room-agent/lg_webos.py",
        without_ssl: bool = False,
        runner: Runner = subprocess.run,
    ) -> None:
        super().__init__(endpoint_id, name, priority, runner=runner, timeout=22.0)
        if not host.strip() or not _valid_host(host.strip()):
            raise DisplayConfigError("LG webOS host is invalid.")
        if not key_file.strip() or len(key_file) > 500:
            raise DisplayConfigError("LG webOS key-file path is invalid.")
        if not python.strip() or not helper.strip():
            raise DisplayConfigError("LG webOS helper paths must not be empty.")
        if not isinstance(without_ssl, bool):
            raise DisplayConfigError("LG webOS legacy-connection flag must be boolean.")
        self.host = host.strip()
        self.key_file = Path(key_file).expanduser()
        self.python = str(Path(python).expanduser())
        self.helper = str(Path(helper).expanduser())
        self.without_ssl = without_ssl
        self._apps: dict[str, str] = {}
        self._browser_app_id: str | None = None
        self._capabilities: frozenset[str] = frozenset()

    def _lg(self, operation: str, *arguments: str) -> dict[str, Any]:
        command = [
            self.python,
            self.helper,
            operation,
            "--host",
            self.host,
            "--key-file",
            str(self.key_file),
        ]
        if self.without_ssl:
            command.append("--without-ssl")
        result = self._run([*command, *arguments])
        if result.returncode != 0:
            raise DisplayControlError(
                f"LG webOS {operation} failed: {self._detail(result)}"
            )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise DisplayControlError(f"LG webOS {operation} returned no result.")
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise DisplayControlError(
                f"LG webOS {operation} returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") not in {
            "ready",
            "paired",
            "executed",
        }:
            raise DisplayControlError(f"LG webOS {operation} was not confirmed.")
        return payload

    @staticmethod
    def _app_aliases(apps: Any) -> tuple[dict[str, str], str | None]:
        aliases: dict[str, str] = {}
        browser_id = None
        if not isinstance(apps, list):
            return aliases, browser_id
        for app in apps:
            if not isinstance(app, dict):
                continue
            app_id = app.get("id")
            title = app.get("title")
            if not isinstance(app_id, str) or not isinstance(title, str):
                continue
            searchable = f"{app_id} {title}".casefold()
            special = None
            if "youtube" in searchable:
                special = "youtube"
            elif "netflix" in searchable:
                special = "netflix"
            elif "browser" in searchable:
                browser_id = app_id
            if bool(app.get("visible", True)):
                slug = _app_slug(title)
                if _APP_SLUG.fullmatch(slug):
                    aliases.setdefault(slug, app_id)
            if special is not None:
                aliases[special] = app_id
        return aliases, browser_id

    def probe(self) -> EndpointStatus:
        if not self.key_file.is_file():
            return EndpointStatus(
                self.endpoint_id,
                self.name,
                self.kind,
                False,
                frozenset(),
                "LG pairing key is missing; run configure_display.py lg",
            )
        try:
            payload = self._lg("probe")
            self._apps, self._browser_app_id = self._app_aliases(payload.get("apps"))
            capabilities = {
                CAP_MEDIA_PAUSE,
                CAP_MEDIA_RESUME,
                CAP_MEDIA_CLOSE,
            }
            capabilities.update(app_capability(app) for app in self._apps)
            if "youtube" in self._apps:
                capabilities.add(CAP_YOUTUBE_PLAY)
            if self._browser_app_id is not None:
                capabilities.update({CAP_WEB_BROWSER, CAP_WEB_SEARCH})
            self._capabilities = frozenset(capabilities)
            return EndpointStatus(
                self.endpoint_id,
                self.name,
                self.kind,
                True,
                self._capabilities,
                f"paired LG webOS TV; {len(self._apps)} launchable app aliases",
            )
        except DisplayControlError as exc:
            self._apps = {}
            self._browser_app_id = None
            self._capabilities = frozenset()
            return EndpointStatus(
                self.endpoint_id,
                self.name,
                self.kind,
                False,
                frozenset(),
                str(exc),
            )

    def launch_app(self, app: str) -> dict[str, str]:
        app_id = self._apps.get(_app_slug(app))
        if app_id is None:
            raise DisplayControlError(f"{app} is not installed on the LG TV.")
        self._lg("launch-app", app_id)
        return {"app": _app_slug(app), "app_id": app_id, "player": "lg-webos"}

    def open_site(self, site: str) -> dict[str, str]:
        if site == "youtube":
            return self.launch_app("youtube")
        if site != "miton" or CAP_WEB_BROWSER not in self._capabilities:
            raise DisplayControlError("The LG TV has no verified browser for this site.")
        url = "https://www.miton.cz/"
        self._lg("open-url", url)
        return {"opened": url, "player": "lg-webos"}

    def search_google(self, query: str) -> dict[str, str]:
        query = " ".join(query.split())
        if (
            CAP_WEB_SEARCH not in self._capabilities
            or not query
            or len(query) > MAX_WEB_QUERY_CHARACTERS
        ):
            raise DisplayControlError("The LG TV has no verified browser for this search.")
        url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
        self._lg("open-url", url)
        return {"opened": url, "player": "lg-webos"}

    def play_youtube(self, video_id: str) -> dict[str, str]:
        app_id = self._apps.get("youtube")
        if (
            app_id is None
            or CAP_YOUTUBE_PLAY not in self._capabilities
            or not _VIDEO_ID.fullmatch(video_id)
        ):
            raise DisplayControlError("Native YouTube playback is unavailable on the LG TV.")
        self._lg("youtube", app_id, video_id)
        return {
            "opened": f"https://www.youtube.com/watch?v={video_id}",
            "app_id": app_id,
            "media_status": "PlaybackRequested",
            "player": "lg-webos",
        }

    def pause(self) -> dict[str, str]:
        self._lg("pause")
        return {"media_status": "PauseRequested", "player": "lg-webos"}

    def resume(self) -> dict[str, str]:
        self._lg("resume")
        return {"media_status": "PlayRequested", "player": "lg-webos"}

    def close(self) -> dict[str, str]:
        self._lg("home")
        return {"media_status": "Home", "player": "lg-webos"}


class SamsungTvEndpoint(CommandEndpoint):
    """Paired Samsung Tizen TV controlled through a restricted helper process."""

    kind = "samsung_tv"

    def __init__(
        self,
        endpoint_id: str,
        name: str,
        priority: int,
        *,
        host: str,
        port: int = 8002,
        token_file: str,
        python: str = "/home/voiceagent/room-agent/.venv/bin/python",
        helper: str = "/home/voiceagent/room-agent/samsung_tizen.py",
        runner: Runner = subprocess.run,
    ) -> None:
        super().__init__(endpoint_id, name, priority, runner=runner, timeout=22.0)
        if not host.strip() or not _valid_host(host.strip()):
            raise DisplayConfigError("Samsung TV host is invalid.")
        if port not in {8001, 8002}:
            raise DisplayConfigError("Samsung TV control port must be 8001 or 8002.")
        if not token_file.strip() or len(token_file) > 500:
            raise DisplayConfigError("Samsung TV token-file path is invalid.")
        if not python.strip() or not helper.strip():
            raise DisplayConfigError("Samsung TV helper paths must not be empty.")
        self.host = host.strip()
        self.port = port
        self.token_file = Path(token_file).expanduser()
        self.python = str(Path(python).expanduser())
        self.helper = str(Path(helper).expanduser())
        self._apps: dict[str, str] = {}
        self._browser_app_id: str | None = None
        self._capabilities: frozenset[str] = frozenset()

    def _samsung(self, operation: str, *arguments: str) -> dict[str, Any]:
        command = [
            self.python,
            self.helper,
            operation,
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--token-file",
            str(self.token_file),
        ]
        result = self._run([*command, *arguments])
        if result.returncode != 0:
            raise DisplayControlError(
                f"Samsung Tizen {operation} failed: {self._detail(result)}"
            )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise DisplayControlError(f"Samsung Tizen {operation} returned no result.")
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise DisplayControlError(
                f"Samsung Tizen {operation} returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") not in {
            "ready",
            "paired",
            "executed",
        }:
            raise DisplayControlError(f"Samsung Tizen {operation} was not confirmed.")
        return payload

    @staticmethod
    def _app_aliases(apps: Any) -> tuple[dict[str, str], str | None]:
        aliases: dict[str, str] = {}
        browser_id = None
        if not isinstance(apps, list):
            return aliases, browser_id
        for app in apps:
            if not isinstance(app, dict):
                continue
            app_id = app.get("id")
            title = app.get("title")
            if not isinstance(app_id, str) or not isinstance(title, str):
                continue
            searchable = f"{app_id} {title}".casefold()
            special = None
            if "youtube" in searchable:
                special = "youtube"
            elif "netflix" in searchable:
                special = "netflix"
            elif "browser" in searchable or app_id == "org.tizen.browser":
                browser_id = app_id
            if bool(app.get("visible", True)):
                slug = _app_slug(title)
                if _APP_SLUG.fullmatch(slug):
                    aliases.setdefault(slug, app_id)
            if special is not None:
                aliases[special] = app_id
        return aliases, browser_id

    def probe(self) -> EndpointStatus:
        if self.port == 8002 and not self.token_file.is_file():
            return EndpointStatus(
                self.endpoint_id,
                self.name,
                self.kind,
                False,
                frozenset(),
                "Samsung pairing token is missing; run configure_display.py samsung",
            )
        try:
            payload = self._samsung("probe")
            self._apps, self._browser_app_id = self._app_aliases(payload.get("apps"))
            capabilities = {
                CAP_MEDIA_PAUSE,
                CAP_MEDIA_RESUME,
                CAP_MEDIA_CLOSE,
            }
            capabilities.update(app_capability(app) for app in self._apps)
            if self._browser_app_id is not None:
                capabilities.update(
                    {CAP_WEB_BROWSER, CAP_WEB_SEARCH, CAP_YOUTUBE_PLAY}
                )
            self._capabilities = frozenset(capabilities)
            return EndpointStatus(
                self.endpoint_id,
                self.name,
                self.kind,
                True,
                self._capabilities,
                f"paired Samsung Tizen TV; {len(self._apps)} launchable app aliases",
            )
        except DisplayControlError as exc:
            self._apps = {}
            self._browser_app_id = None
            self._capabilities = frozenset()
            return EndpointStatus(
                self.endpoint_id,
                self.name,
                self.kind,
                False,
                frozenset(),
                str(exc),
            )

    def launch_app(self, app: str) -> dict[str, str]:
        alias = _app_slug(app)
        app_id = self._apps.get(alias)
        if app_id is None:
            raise DisplayControlError(f"{app} is not installed on the Samsung TV.")
        self._samsung("launch-app", app_id)
        return {"app": alias, "app_id": app_id, "player": "samsung-tizen"}

    def open_site(self, site: str) -> dict[str, str]:
        if site == "youtube":
            return self.launch_app("youtube")
        if site != "miton" or CAP_WEB_BROWSER not in self._capabilities:
            raise DisplayControlError(
                "The Samsung TV has no verified browser for this site."
            )
        url = "https://www.miton.cz/"
        self._samsung("open-url", url)
        return {"opened": url, "player": "samsung-tizen"}

    def search_google(self, query: str) -> dict[str, str]:
        query = " ".join(query.split())
        if (
            CAP_WEB_SEARCH not in self._capabilities
            or not query
            or len(query) > MAX_WEB_QUERY_CHARACTERS
        ):
            raise DisplayControlError(
                "The Samsung TV has no verified browser for this search."
            )
        url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
        self._samsung("open-url", url)
        return {"opened": url, "player": "samsung-tizen"}

    def play_youtube(self, video_id: str) -> dict[str, str]:
        if (
            CAP_YOUTUBE_PLAY not in self._capabilities
            or not _VIDEO_ID.fullmatch(video_id)
        ):
            raise DisplayControlError(
                "Exact YouTube playback is unavailable on this Samsung TV."
            )
        self._samsung("youtube", video_id)
        return {
            "opened": f"https://www.youtube.com/watch?v={video_id}",
            "media_status": "PlaybackRequested",
            "player": "samsung-tizen-browser",
        }

    def _key(self, key: str, status: str) -> dict[str, str]:
        self._samsung("key", key)
        return {"media_status": status, "player": "samsung-tizen"}

    def pause(self) -> dict[str, str]:
        return self._key("KEY_PAUSE", "PauseRequested")

    def resume(self) -> dict[str, str]:
        return self._key("KEY_PLAY", "PlayRequested")

    def close(self) -> dict[str, str]:
        return self._key("KEY_HOME", "Home")


class DisplayManager:
    """Choose the highest-priority healthy endpoint for each exact capability."""

    # execute_local_tv_action checks this. HDMI home restoration is handled by
    # HdmiDisplayEndpoint.close so native endpoints never reveal the Pi desktop.
    home_url = None

    def __init__(
        self,
        hdmi_player: Any,
        endpoints: Sequence[DisplayEndpoint] = (),
        *,
        probe_cache_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= probe_cache_seconds <= 300:
            raise DisplayConfigError("Display probe cache must be between 0 and 300 seconds.")
        self.hdmi = HdmiDisplayEndpoint(hdmi_player)
        all_endpoints = [*endpoints, self.hdmi]
        ids = [endpoint.endpoint_id for endpoint in all_endpoints]
        if len(ids) != len(set(ids)):
            raise DisplayConfigError("Display endpoint ids must be unique.")
        self.endpoints = tuple(all_endpoints)
        self.probe_cache_seconds = probe_cache_seconds
        self.clock = clock
        self._cache: dict[str, tuple[float, EndpointStatus]] = {}
        self.active_endpoint_id: str | None = None

    def _status(self, endpoint: DisplayEndpoint, *, force: bool = False) -> EndpointStatus:
        cached = self._cache.get(endpoint.endpoint_id)
        now = self.clock()
        if not force and cached is not None and now - cached[0] <= self.probe_cache_seconds:
            return cached[1]
        status = endpoint.probe()
        self._cache[endpoint.endpoint_id] = (now, status)
        return status

    def invalidate(self, endpoint_id: str) -> None:
        self._cache.pop(endpoint_id, None)

    def statuses(self, *, force: bool = False) -> list[EndpointStatus]:
        return [self._status(endpoint, force=force) for endpoint in self.endpoints]

    def native_ready(self, *, force: bool = False) -> bool:
        native = sorted(
            (endpoint for endpoint in self.endpoints if endpoint.kind != "pi_hdmi"),
            key=lambda item: item.priority,
            reverse=True,
        )
        for endpoint in native:
            status = self._status(endpoint, force=force)
            if status.online and status.capabilities:
                return True
        return False

    def _ordered_endpoints(self, *, affinity: bool = False) -> list[DisplayEndpoint]:
        if affinity:
            if self.active_endpoint_id is None:
                raise DisplayControlError("There is no active display session.")
            return [
                endpoint
                for endpoint in self.endpoints
                if endpoint.endpoint_id == self.active_endpoint_id
            ]
        return sorted(self.endpoints, key=lambda item: item.priority, reverse=True)

    def _execute(
        self,
        capability: str,
        method: str,
        *arguments: str,
        affinity: bool = False,
        activate: bool = True,
        allow_fallback: bool = True,
    ) -> dict[str, str]:
        failures: list[tuple[str, str]] = []
        capability_found = False
        for endpoint in self._ordered_endpoints(affinity=affinity):
            status = self._status(endpoint)
            if not status.online or capability not in status.capabilities:
                continue
            capability_found = True
            try:
                result = getattr(endpoint, method)(*arguments)
            except DisplayControlError as exc:
                self.invalidate(endpoint.endpoint_id)
                failures.append((endpoint.name, str(exc)))
                if affinity or not allow_fallback:
                    break
                continue
            response = dict(result)
            response.update(
                {
                    "display_endpoint": endpoint.endpoint_id,
                    "display_name": endpoint.name,
                    "display_kind": endpoint.kind,
                }
            )
            if failures:
                response["fallback_from"] = ", ".join(name for name, _ in failures)
                response["fallback_reason"] = "; ".join(
                    f"{name}: {detail}" for name, detail in failures
                )
            if activate:
                self.active_endpoint_id = endpoint.endpoint_id
            return response
        detail = "; ".join(f"{name}: {reason}" for name, reason in failures)
        if detail:
            raise DisplayControlError(detail)
        if not capability_found:
            raise DisplayControlError(
                f"No enrolled online display exposes capability {capability}."
            )
        raise DisplayControlError(f"Display action {method} failed.")

    def show_home(self) -> dict[str, str]:
        return self.hdmi._call("show_home")

    def open_site(self, site: str) -> dict[str, str]:
        capability = CAP_APP_YOUTUBE if site == "youtube" else CAP_WEB_BROWSER
        return self._execute(capability, "open_site", site)

    def launch_app(self, app: str) -> dict[str, str]:
        app = _app_slug(app)
        return self._execute(app_capability(app), "launch_app", app)

    def search_google(self, query: str) -> dict[str, str]:
        return self._execute(CAP_WEB_SEARCH, "search_google", query)

    def open_first_google_result(self) -> dict[str, str]:
        return self._execute(
            CAP_WEB_OPEN_FIRST,
            "open_first_google_result",
            affinity=True,
            allow_fallback=False,
        )

    def play_youtube(self, video_id: str) -> dict[str, str]:
        return self._execute(CAP_YOUTUBE_PLAY, "play_youtube", video_id)

    def pause(self) -> dict[str, str]:
        return self._execute(
            CAP_MEDIA_PAUSE,
            "pause",
            affinity=True,
            allow_fallback=False,
        )

    def resume(self) -> dict[str, str]:
        return self._execute(
            CAP_MEDIA_RESUME,
            "resume",
            affinity=True,
            allow_fallback=False,
        )

    def close(self) -> dict[str, str]:
        result = self._execute(
            CAP_MEDIA_CLOSE,
            "close",
            affinity=True,
            activate=False,
            allow_fallback=False,
        )
        self.active_endpoint_id = None
        return result


def _mapping(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DisplayConfigError(f"{description} must be a JSON object.")
    return value


def load_display_manager(config_path: Path, hdmi_player: Any) -> DisplayManager:
    """Load enrolled endpoints. A missing file intentionally means HDMI-only."""

    if not config_path.is_file():
        return DisplayManager(hdmi_player)
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise DisplayConfigError(f"Display configuration could not be read: {exc}") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise DisplayConfigError("Display configuration exceeds the size limit.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DisplayConfigError("Display configuration is not valid UTF-8 JSON.") from exc
    root = _mapping(payload, "Display configuration")
    if root.get("version") != CONFIG_VERSION:
        raise DisplayConfigError(f"Display configuration version must be {CONFIG_VERSION}.")
    raw_endpoints = root.get("endpoints", [])
    if not isinstance(raw_endpoints, list) or len(raw_endpoints) > MAX_ENDPOINTS:
        raise DisplayConfigError(f"Display configuration supports at most {MAX_ENDPOINTS} endpoints.")
    settings = _mapping(root.get("settings", {}), "Display settings")
    cache_seconds = settings.get("probe_cache_seconds", 60.0)
    if not isinstance(cache_seconds, (int, float)) or isinstance(cache_seconds, bool):
        raise DisplayConfigError("Display probe cache must be numeric.")

    endpoints: list[DisplayEndpoint] = []
    for index, raw_endpoint in enumerate(raw_endpoints):
        data = _mapping(raw_endpoint, f"Display endpoint {index}")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise DisplayConfigError(f"Display endpoint {index} enabled flag must be boolean.")
        if not enabled:
            continue
        kind = data.get("kind")
        endpoint_id = data.get("id")
        name = data.get("name")
        priority = data.get("priority", 100)
        if (
            not isinstance(endpoint_id, str)
            or not isinstance(name, str)
            or not isinstance(priority, int)
            or isinstance(priority, bool)
        ):
            raise DisplayConfigError(f"Display endpoint {index} has invalid identity fields.")
        if kind == "android_tv":
            adb = data.get("adb", "adb")
            apps = data.get("apps")
            connect_on_probe = data.get("connect_on_probe", True)
            if (
                not isinstance(adb, str)
                or not isinstance(connect_on_probe, bool)
                or (apps is not None and not isinstance(apps, dict))
                or (
                    isinstance(apps, dict)
                    and any(
                        not isinstance(app, str) or not isinstance(package, str)
                        for app, package in apps.items()
                    )
                )
            ):
                raise DisplayConfigError("Android TV adb settings are invalid.")
            endpoints.append(
                AndroidTvEndpoint(
                    endpoint_id,
                    name,
                    priority,
                    serial=data.get("serial") if isinstance(data.get("serial"), str) else None,
                    host=data.get("host") if isinstance(data.get("host"), str) else None,
                    adb=adb,
                    apps=apps,
                    connect_on_probe=connect_on_probe,
                )
            )
        elif kind == "apple_tv":
            host = data.get("host")
            identifier = data.get("identifier")
            if not isinstance(host, str) or not isinstance(identifier, str):
                raise DisplayConfigError("Apple TV endpoints require host and identifier.")
            atvremote = data.get("atvremote", "atvremote")
            if not isinstance(atvremote, str):
                raise DisplayConfigError("Apple TV atvremote path must be text.")
            endpoints.append(
                AppleTvEndpoint(
                    endpoint_id,
                    name,
                    priority,
                    host=host,
                    identifier=identifier,
                    atvremote=atvremote,
                )
            )
        elif kind == "samsung_tv":
            host = data.get("host")
            port = data.get("port")
            legacy_ports = data.get("ports")
            if port is None and isinstance(legacy_ports, list) and legacy_ports:
                port = legacy_ports[0]
            if port is None:
                port = 8002
            token_file = data.get(
                "token_file", "/home/voiceagent/.config/room-agent/samsung-token.txt"
            )
            python = data.get(
                "python", "/home/voiceagent/room-agent/.venv/bin/python"
            )
            helper = data.get(
                "helper", "/home/voiceagent/room-agent/samsung_tizen.py"
            )
            if (
                not isinstance(host, str)
                or not isinstance(port, int)
                or isinstance(port, bool)
                or not all(
                    isinstance(value, str)
                    for value in (token_file, python, helper)
                )
            ):
                raise DisplayConfigError(
                    "Samsung TV endpoints require host, port, token-file and helper paths."
                )
            endpoints.append(
                SamsungTvEndpoint(
                    endpoint_id,
                    name,
                    priority,
                    host=host,
                    port=port,
                    token_file=token_file,
                    python=python,
                    helper=helper,
                )
            )
        elif kind == "lg_webos":
            host = data.get("host")
            key_file = data.get("key_file")
            python = data.get(
                "python", "/home/voiceagent/room-agent/.venv/bin/python"
            )
            helper = data.get(
                "helper", "/home/voiceagent/room-agent/lg_webos.py"
            )
            without_ssl = data.get("without_ssl", False)
            if not all(
                isinstance(value, str) for value in (host, key_file, python, helper)
            ) or not isinstance(without_ssl, bool):
                raise DisplayConfigError(
                    "LG webOS endpoints require host, key-file and helper paths."
                )
            endpoints.append(
                LgWebOsEndpoint(
                    endpoint_id,
                    name,
                    priority,
                    host=host,
                    key_file=key_file,
                    python=python,
                    helper=helper,
                    without_ssl=without_ssl,
                )
            )
        else:
            raise DisplayConfigError(f"Unsupported display endpoint kind: {kind!r}")
    return DisplayManager(
        hdmi_player,
        endpoints,
        probe_cache_seconds=float(cache_seconds),
    )


def status_document(manager: DisplayManager, *, force: bool = True) -> dict[str, Any]:
    statuses = manager.statuses(force=force)
    return {
        "version": 1,
        "native_ready": any(
            status.online and bool(status.capabilities) and status.kind != "pi_hdmi"
            for status in statuses
        ),
        "endpoints": [status.as_dict() for status in statuses],
    }

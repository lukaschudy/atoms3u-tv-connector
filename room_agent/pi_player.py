"""Allowlisted Chromium player for the Raspberry Pi HDMI display."""

from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Mapping


_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ALLOWED_SITES = {
    "miton": "https://www.miton.cz/",
    "youtube": "https://www.youtube.com/",
}
MAX_WEB_QUERY_CHARACTERS = 120


class PiPlayerError(RuntimeError):
    pass


class ChromiumHdmiPlayer:
    """Launch allowlisted YouTube pages in Chromium on the Pi's Wayland display."""

    def __init__(
        self,
        *,
        chromium: str | None = None,
        profile_dir: Path | None = None,
        extension_dir: Path | None = None,
        runtime_dir: Path | None = None,
        wayland_display: str = "wayland-0",
        base_environment: Mapping[str, str] | None = None,
        launcher: Callable[..., subprocess.Popen] = subprocess.Popen,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        killer: Callable[[int, int], None] = os.kill,
        group_killer: Callable[[int, int], None] | None = None,
        proc_root: Path = Path("/proc"),
        busctl: str | None = None,
        auto_play: bool = True,
        media_timeout: float = 12.0,
        home_url: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        uid = getattr(os, "getuid", lambda: 1000)()
        self.chromium = chromium
        self.profile_dir = (
            profile_dir or Path.home() / ".config" / "room-agent" / "chromium"
        )
        self.extension_dir = extension_dir or Path(__file__).with_name(
            "youtube_kiosk_extension"
        )
        self.runtime_dir = runtime_dir or Path(f"/run/user/{uid}")
        self.wayland_display = wayland_display
        environment = os.environ if base_environment is None else base_environment
        self.base_environment = dict(environment)
        self.launcher = launcher
        self.runner = runner
        self.killer = killer
        self.group_killer = group_killer or getattr(os, "killpg", None)
        self.proc_root = proc_root
        self.busctl = busctl
        self.auto_play = auto_play
        self.media_timeout = media_timeout
        self.home_url = self._validate_home_url(home_url) if home_url else None
        self.clock = clock
        self.sleeper = sleeper
        self.last_pid: int | None = None
        self.last_google_query: str | None = None
        self._launched_process_groups: set[int] = set()

    @staticmethod
    def _validate_home_url(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("The HDMI home page must be a loopback HTTP URL.")
        return url

    def _chromium_binary(self) -> str:
        binary = (
            self.chromium
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
        )
        if not binary:
            raise PiPlayerError("Chromium is not installed on the Raspberry Pi.")
        return binary

    def _busctl_binary(self) -> str:
        binary = self.busctl or shutil.which("busctl")
        if not binary:
            raise PiPlayerError("The Pi media-control tool busctl is unavailable.")
        return binary

    def is_available(self) -> tuple[bool, str]:
        """Probe HDMI fallback prerequisites without opening a browser window."""

        if not self.runtime_dir.is_dir():
            return False, "Pi graphical session is not active"
        try:
            self._chromium_binary()
        except PiPlayerError as exc:
            return False, str(exc)
        if not (self.extension_dir / "manifest.json").is_file():
            return False, "Pi browser kiosk helper is missing"
        return True, f"Pi HDMI desktop ready on {self.wayland_display}"

    def _session_environment(self) -> dict[str, str]:
        environment = self.base_environment.copy()
        environment.update(
            {
                "XDG_RUNTIME_DIR": str(self.runtime_dir),
                "WAYLAND_DISPLAY": self.wayland_display,
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.runtime_dir / 'bus'}",
            }
        )
        return environment

    def _run_busctl(self, arguments: list[str]) -> subprocess.CompletedProcess:
        try:
            return self.runner(
                [self._busctl_binary(), "--user", *arguments],
                capture_output=True,
                text=True,
                timeout=5,
                env=self._session_environment(),
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise PiPlayerError(f"Pi media control failed: {exc}") from exc

    def _mpris_player(self) -> str | None:
        result = self._run_busctl(["--no-legend", "--list"])
        if result.returncode != 0:
            return None
        names = [
            line.split()[0]
            for line in result.stdout.splitlines()
            if line.startswith("org.mpris.MediaPlayer2.chromium.instance")
        ]
        if self.last_pid is not None:
            expected = f"org.mpris.MediaPlayer2.chromium.instance{self.last_pid}"
            if expected in names:
                return expected
        return names[-1] if names else None

    def _playback_status(self, player_name: str) -> str | None:
        result = self._run_busctl(
            [
                "get-property",
                player_name,
                "/org/mpris/MediaPlayer2",
                "org.mpris.MediaPlayer2.Player",
                "PlaybackStatus",
            ]
        )
        if result.returncode != 0:
            return None
        match = re.search(r'"(Playing|Paused|Stopped)"', result.stdout)
        return match.group(1) if match else None

    def _control_media(self, method: str, expected_status: str) -> dict[str, str]:
        deadline = self.clock() + self.media_timeout
        last_detail = "Chromium has not exposed a media session yet."
        while True:
            player_name = self._mpris_player()
            if player_name is not None:
                result = self._run_busctl(
                    [
                        "call",
                        player_name,
                        "/org/mpris/MediaPlayer2",
                        "org.mpris.MediaPlayer2.Player",
                        method,
                    ]
                )
                if result.returncode == 0:
                    status = self._playback_status(player_name)
                    if status == expected_status:
                        return {"media_status": status, "media_player": player_name}
                    last_detail = f"Chromium reported {status or 'no playback state'}."
                else:
                    last_detail = (result.stderr or result.stdout or "busctl failed").strip()
            if self.clock() >= deadline:
                raise PiPlayerError(
                    f"Chromium did not reach {expected_status}: {last_detail}"
                )
            self.sleeper(0.4)

    def pause(self) -> dict[str, str]:
        return self._control_media("Pause", "Paused")

    def resume(self) -> dict[str, str]:
        return self._control_media("Play", "Playing")

    def _profile_chromium_pids(self) -> list[int]:
        profile_argument = f"--user-data-dir={self.profile_dir}"
        pids = []
        try:
            entries = self.proc_root.iterdir()
        except OSError as exc:
            raise PiPlayerError(f"Cannot inspect the Pi browser processes: {exc}") from exc

        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                arguments = [
                    value.decode("utf-8", errors="replace")
                    for value in (entry / "cmdline").read_bytes().split(b"\0")
                    if value
                ]
            except (OSError, ValueError):
                continue
            if not arguments:
                continue
            executable = Path(arguments[0]).name.casefold()
            is_browser = "chromium" in executable or executable in {
                "chrome",
                "google-chrome",
            }
            is_child = any(value.startswith("--type=") for value in arguments)
            if is_browser and not is_child and profile_argument in arguments:
                pids.append(int(entry.name))
        return pids

    def close(self) -> dict[str, str]:
        pids = self._profile_chromium_pids()
        process_groups = sorted(self._launched_process_groups)
        failures = []

        if self.group_killer is not None:
            for process_group in process_groups:
                try:
                    self.group_killer(process_group, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except (OSError, ValueError) as exc:
                    failures.append(f"process group {process_group}: {exc}")

        for pid in pids:
            try:
                self.killer(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except (OSError, ValueError) as exc:
                failures.append(f"process {pid}: {exc}")

        deadline = self.clock() + self.media_timeout
        while self._profile_chromium_pids():
            if self.clock() >= deadline:
                detail = f" ({'; '.join(failures)})" if failures else ""
                raise PiPlayerError(
                    f"The Pi kiosk did not close before the timeout{detail}."
                )
            self.sleeper(0.1)

        self.last_pid = None
        self.last_google_query = None
        self._launched_process_groups.clear()
        return {
            "media_status": "Closed",
            "closed_pids": ",".join(str(pid) for pid in pids),
            "closed_process_groups": ",".join(
                str(process_group) for process_group in process_groups
            ),
        }

    def _launch_url(
        self,
        url: str,
        *,
        start_playback: bool,
        kiosk_helper: bool,
    ) -> dict[str, str]:
        if not self.runtime_dir.is_dir():
            raise PiPlayerError(
                "The Pi HDMI desktop is not active; its Wayland runtime is unavailable."
            )
        if kiosk_helper and not (self.extension_dir / "manifest.json").is_file():
            raise PiPlayerError(
                f"The browser kiosk helper is missing: {self.extension_dir}"
            )

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self._chromium_binary(),
            "--ozone-platform=wayland",
            "--kiosk",
            "--no-first-run",
            "--disable-session-crashed-bubble",
            "--autoplay-policy=no-user-gesture-required",
            "--password-store=basic",
        ]
        if kiosk_helper:
            command.extend(
                [
                    f"--disable-extensions-except={self.extension_dir}",
                    f"--load-extension={self.extension_dir}",
                ]
            )
        command.extend([f"--user-data-dir={self.profile_dir}", url])
        environment = self._session_environment()

        try:
            process = self.launcher(
                command,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise PiPlayerError(f"Chromium could not be started: {exc}") from exc

        self.last_pid = process.pid
        self._launched_process_groups.add(process.pid)
        media = self.resume() if start_playback and self.auto_play else {}

        return {
            "opened": url,
            "player": "pi-hdmi-chromium",
            "pid": str(process.pid),
            **media,
        }

    def open_youtube(self) -> dict[str, str]:
        return self.open_site("youtube")

    def show_home(self) -> dict[str, str]:
        if self.home_url is None:
            raise PiPlayerError("The Pi HDMI home page is not configured.")
        self.last_google_query = None
        return self._launch_url(
            self.home_url,
            start_playback=False,
            # Chromium reuses this profile when media opens later, so load the
            # narrowly scoped Google/YouTube helper in the initial process.
            kiosk_helper=True,
        )

    def open_site(self, site: str) -> dict[str, str]:
        url = _ALLOWED_SITES.get(site)
        if url is None:
            raise PiPlayerError("Refusing to open a site outside the allowlist.")
        result = self._launch_url(
            url,
            start_playback=False,
            kiosk_helper=site == "youtube",
        )
        self.last_google_query = None
        return result

    def search_google(self, query: str) -> dict[str, str]:
        query = " ".join(query.split())
        if not query or len(query) > MAX_WEB_QUERY_CHARACTERS:
            raise PiPlayerError("Refusing to open an invalid Google search query.")
        url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
        result = self._launch_url(
            url,
            start_playback=False,
            kiosk_helper=True,
        )
        self.last_google_query = query
        return result

    def open_first_google_result(self) -> dict[str, str]:
        if self.last_google_query is None:
            raise PiPlayerError("There is no active Google search to continue.")
        url = "https://www.google.com/search?" + urllib.parse.urlencode(
            {"q": self.last_google_query}
        )
        return self._launch_url(
            url + "#room-agent-open-first",
            start_playback=False,
            kiosk_helper=True,
        )

    def play_youtube(self, video_id: str) -> dict[str, str]:
        if not _VIDEO_ID_PATTERN.fullmatch(video_id):
            raise PiPlayerError("Refusing to open an invalid YouTube video ID.")
        result = self._launch_url(
            f"https://www.youtube.com/watch?v={video_id}&autoplay=1",
            start_playback=True,
            kiosk_helper=True,
        )
        self.last_google_query = None
        return result

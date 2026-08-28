"""Dedicated, allowlisted Chromium session controlled by the Windows host."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path


ALLOWED_SITES = {
    "miton": "https://www.miton.cz/",
    "youtube": "https://www.youtube.com/",
}
MAX_WEB_QUERY_CHARACTERS = 120
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WM_CLOSE = 0x0010


class WindowsBrowserError(RuntimeError):
    pass


def default_windows_state_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "RoomAgent"
    return Path.home() / ".config" / "room-agent"


class WindowsBrowserController:
    """Own one isolated browser profile and never touch unrelated browser tabs."""

    def __init__(
        self,
        *,
        browser: Path | None = None,
        profile_dir: Path | None = None,
        extension_dir: Path | None = None,
        launcher: Callable[..., subprocess.Popen] = subprocess.Popen,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        media_key_sender: Callable[[], None] | None = None,
        window_activator: Callable[[int], bool] | None = None,
        personal_profile: bool = False,
        window_snapshotter: Callable[[], set[int]] | None = None,
        window_handle_activator: Callable[[int], bool] | None = None,
        tracked_window_closer: Callable[[int], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.browser = browser or self._find_browser()
        self.profile_dir = profile_dir or default_windows_state_dir() / "browser"
        self.extension_dir = extension_dir or Path(__file__).with_name(
            "youtube_kiosk_extension"
        )
        self.launcher = launcher
        self.runner = runner
        self.media_key_sender = media_key_sender or self._send_media_key
        self.window_activator = window_activator or self._activate_window
        self.personal_profile = personal_profile
        self.window_snapshotter = window_snapshotter or self._browser_windows
        self.window_handle_activator = (
            window_handle_activator or self._activate_window_handle
        )
        self.tracked_window_closer = tracked_window_closer or self._close_window
        self.sleeper = sleeper
        self._process: subprocess.Popen | None = None
        self._window_handle: int | None = None
        self._media_state: str | None = None
        self._last_google_query: str | None = None

    @staticmethod
    def _find_browser() -> Path:
        candidates = [
            shutil.which("brave"),
            str(
                Path(os.getenv("PROGRAMFILES", ""))
                / "BraveSoftware"
                / "Brave-Browser"
                / "Application"
                / "brave.exe"
            ),
            shutil.which("chrome"),
            str(
                Path(os.getenv("PROGRAMFILES", ""))
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe"
            ),
            shutil.which("msedge"),
            str(
                Path(os.getenv("PROGRAMFILES(X86)", ""))
                / "Microsoft"
                / "Edge"
                / "Application"
                / "msedge.exe"
            ),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        raise WindowsBrowserError("Microsoft Edge or Google Chrome was not found.")

    @staticmethod
    def _send_media_key() -> None:
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
            user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)
        except (AttributeError, OSError) as exc:
            raise WindowsBrowserError(f"Windows media control failed: {exc}") from exc

    @staticmethod
    def _activate_window(process_id: int) -> bool:
        """Restore and foreground the visible top-level window owned by a process."""

        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        matching: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @callback_type
        def collect(window: int, _parameter: int) -> bool:
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value == process_id and user32.IsWindowVisible(window):
                matching.append(window)
            return True

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            matching.clear()
            user32.EnumWindows(collect, 0)
            if matching:
                return WindowsBrowserController._activate_window_handle(matching[0])
            time.sleep(0.1)
        return False

    @staticmethod
    def _activate_window_handle(window: int) -> bool:
        if os.name != "nt" or not window:
            return False
        user32 = ctypes.windll.user32
        if not user32.IsWindow(window):
            return False
        user32.ShowWindowAsync(window, SW_RESTORE)
        if user32.SetForegroundWindow(window):
            return True

        # Windows can deny focus to background processes. A short Alt
        # transition and topmost pulse affects only the tracked browser window.
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetWindowPos(
            window,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        user32.SetWindowPos(
            window,
            HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        user32.SetForegroundWindow(window)
        return user32.GetForegroundWindow() == window

    def _browser_windows(self) -> set[int]:
        """Return visible top-level windows owned by this browser executable."""

        if os.name != "nt":
            return set()
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        expected = os.path.normcase(os.path.abspath(str(self.browser)))
        matches: set[int] = set()
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @callback_type
        def collect(window: int, _parameter: int) -> bool:
            if not user32.IsWindowVisible(window):
                return True
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
            process = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                process_id.value,
            )
            if not process:
                return True
            try:
                length = ctypes.c_ulong(32_768)
                image = ctypes.create_unicode_buffer(length.value)
                if kernel32.QueryFullProcessImageNameW(
                    process, 0, image, ctypes.byref(length)
                ):
                    candidate = os.path.normcase(os.path.abspath(image.value))
                    if candidate == expected:
                        matches.add(int(window))
            finally:
                kernel32.CloseHandle(process)
            return True

        user32.EnumWindows(collect, 0)
        return matches

    @staticmethod
    def _close_window(window: int) -> bool:
        if os.name != "nt" or not window:
            return False
        user32 = ctypes.windll.user32
        return bool(
            user32.IsWindow(window)
            and user32.PostMessageW(window, WM_CLOSE, 0, 0)
        )

    def _launch_url(self, url: str, *, media_state: str | None = None) -> dict[str, str]:
        if not self.personal_profile and not (self.extension_dir / "manifest.json").is_file():
            raise WindowsBrowserError(
                f"The Room Agent browser helper is missing: {self.extension_dir}"
            )
        self.close(ignore_missing=True)
        previous_windows = self.window_snapshotter() if self.personal_profile else set()
        if self.personal_profile:
            command = [str(self.browser), "--new-window", url]
        else:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            command = [
                str(self.browser),
                f"--user-data-dir={self.profile_dir}",
                f"--disable-extensions-except={self.extension_dir}",
                f"--load-extension={self.extension_dir}",
                "--no-first-run",
                "--new-window",
                "--disable-session-crashed-bubble",
                "--autoplay-policy=no-user-gesture-required",
                url,
            ]
        try:
            self._process = self.launcher(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except (OSError, ValueError) as exc:
            raise WindowsBrowserError(f"The Room Agent browser could not start: {exc}") from exc
        self._media_state = media_state
        self.sleeper(0.35)
        if self.personal_profile:
            deadline = time.monotonic() + 3.0
            new_windows: set[int] = set()
            while time.monotonic() < deadline:
                new_windows = self.window_snapshotter() - previous_windows
                if new_windows:
                    break
                self.sleeper(0.1)
            if not new_windows:
                self._process = None
                self._media_state = None
                raise WindowsBrowserError(
                    "Brave opened the page but Room Agent could not identify its new window."
                )
            self._window_handle = max(new_windows)
            foregrounded = self.window_handle_activator(self._window_handle)
        else:
            foregrounded = self.window_activator(self._process.pid)
        return {
            "opened": url,
            "player": (
                "windows-personal-browser"
                if self.personal_profile
                else "windows-dedicated-browser"
            ),
            "pid": str(self._process.pid),
            "foregrounded": str(foregrounded).lower(),
        }

    def open_site(self, site: str) -> dict[str, str]:
        url = ALLOWED_SITES.get(site)
        if url is None:
            raise WindowsBrowserError("The requested site is not allowlisted.")
        self._last_google_query = None
        return self._launch_url(url)

    def search_google(self, query: str) -> dict[str, str]:
        query = " ".join(query.split())
        if not query or len(query) > MAX_WEB_QUERY_CHARACTERS:
            raise WindowsBrowserError("The Google query is invalid.")
        url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
        result = self._launch_url(url)
        self._last_google_query = query
        return result

    def open_first_google_result(self) -> dict[str, str]:
        if self._last_google_query is None:
            raise WindowsBrowserError("There is no active Google search to continue.")
        url = "https://www.google.com/search?" + urllib.parse.urlencode(
            {"q": self._last_google_query}
        )
        if self.personal_profile:
            return self._launch_url(url + "&btnI=1")
        return self._launch_url(url + "#room-agent-open-first")

    def play_youtube(self, video_id: str) -> dict[str, str]:
        if not video_id or len(video_id) != 11 or not all(
            character.isalnum() or character in "-_" for character in video_id
        ):
            raise WindowsBrowserError("The YouTube video id is invalid.")
        self._last_google_query = None
        return self._launch_url(
            f"https://www.youtube.com/watch?v={video_id}&autoplay=1",
            media_state="playing",
        )

    def pause(self) -> dict[str, str]:
        if self._media_state is None:
            raise WindowsBrowserError("There is no active Room Agent playback session.")
        if self._media_state != "paused":
            self.media_key_sender()
            self._media_state = "paused"
        return {"media_status": "Paused"}

    def resume(self) -> dict[str, str]:
        if self._media_state is None:
            raise WindowsBrowserError("There is no active Room Agent playback session.")
        if self._media_state != "playing":
            self.media_key_sender()
            self._media_state = "playing"
        return {"media_status": "Playing"}

    def close(self, *, ignore_missing: bool = False) -> dict[str, str]:
        if self.personal_profile:
            window = self._window_handle
            if window is None:
                if ignore_missing:
                    return {"media_status": "Closed"}
                raise WindowsBrowserError("There is no active Room Agent browser session.")
            if not self.tracked_window_closer(window):
                raise WindowsBrowserError("The Room Agent Brave window did not close.")
            self._window_handle = None
            self._process = None
            self._media_state = None
            self._last_google_query = None
            return {"media_status": "Closed", "closed_window": str(window)}

        process = self._process
        if process is None:
            if ignore_missing:
                return {"media_status": "Closed"}
            raise WindowsBrowserError("There is no active Room Agent browser session.")
        if process.poll() is None:
            result = self.runner(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode != 0 and process.poll() is None:
                detail = (result.stderr or result.stdout or "taskkill failed").strip()
                raise WindowsBrowserError(f"The Room Agent browser did not close: {detail}")
        self._process = None
        self._media_state = None
        self._last_google_query = None
        return {"media_status": "Closed", "closed_pid": str(process.pid)}

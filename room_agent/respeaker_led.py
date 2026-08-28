"""Non-critical ReSpeaker USB LED observer for room-agent state events."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from agent_state import AgentState, StateEvent


USB_VENDOR_ID = 0x2886
USB_PRODUCT_ID = 0x0018
USB_CONTROL_OUT_VENDOR_DEVICE = 0x40
USB_CONTROL_INDEX = 0x1C
COMMAND_MONO_COLOR = 0x01
COMMAND_LISTEN = 0x02
COMMAND_CUSTOM = 0x06
COMMAND_BRIGHTNESS = 0x20
USB_TIMEOUT_MS = 1_000
PIXEL_COUNT = 12

STATE_COLORS = {
    AgentState.STARTING: 0x101010,
    AgentState.READY: 0x001818,
    AgentState.LISTENING: 0x0060FF,
    AgentState.TRANSCRIBING: 0xFF8C00,
    AgentState.ROUTING: 0x8000FF,
    AgentState.EXECUTING: 0xC000FF,
    AgentState.SUCCESS: 0x00FF40,
    AgentState.ERROR: 0xFF0000,
    AgentState.STOPPED: 0x000000,
}


class LedUnavailable(RuntimeError):
    pass


class ReSpeakerLedObserver:
    """Render agent state and disable itself after the first USB failure."""

    def __init__(
        self,
        device: Any | None = None,
        *,
        brightness: int = 10,
        finder: Callable[..., Any] | None = None,
    ) -> None:
        if not 0 <= brightness <= 31:
            raise ValueError("LED brightness must be between 0 and 31.")

        if device is None:
            if finder is None:
                try:
                    import usb.core
                except ImportError as exc:
                    raise LedUnavailable(
                        "PyUSB is not installed; install the Debian package python3-usb."
                    ) from exc
                finder = usb.core.find
            try:
                device = finder(idVendor=USB_VENDOR_ID, idProduct=USB_PRODUCT_ID)
            except Exception as exc:
                raise LedUnavailable(
                    f"Cannot search for the ReSpeaker USB interface: {exc}"
                ) from exc

        if device is None:
            raise LedUnavailable(
                "ReSpeaker USB 4 Mic Array LED interface 2886:0018 was not found."
            )

        self._device = device
        self._enabled = True
        try:
            self._write(COMMAND_BRIGHTNESS, [brightness])
        except Exception as exc:
            self._enabled = False
            raise LedUnavailable(
                "The ReSpeaker was found but LED control was denied; install the scoped udev rule."
            ) from exc

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _write(self, command: int, data: Sequence[int]) -> None:
        self._device.ctrl_transfer(
            USB_CONTROL_OUT_VENDOR_DEVICE,
            0,
            command,
            USB_CONTROL_INDEX,
            list(data),
            USB_TIMEOUT_MS,
        )

    def set_color(self, rgb: int) -> None:
        if not 0 <= rgb <= 0xFFFFFF:
            raise ValueError("LED color must be a 24-bit RGB integer.")
        self._write(
            COMMAND_MONO_COLOR,
            [(rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF, 0],
        )

    def listen_for_direction(self) -> None:
        """Let the ReSpeaker firmware display its VAD/DOA listening pattern."""
        self._write(COMMAND_LISTEN, [0])

    def show_pixels(self, colors: Sequence[int]) -> None:
        """Set all 12 ring pixels from 24-bit RGB integers."""
        if len(colors) != PIXEL_COUNT:
            raise ValueError(f"Exactly {PIXEL_COUNT} LED colors are required.")

        data: list[int] = []
        for rgb in colors:
            if not 0 <= rgb <= 0xFFFFFF:
                raise ValueError("Every LED color must be a 24-bit RGB integer.")
            data.extend([(rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF, 0])
        self._write(COMMAND_CUSTOM, data)

    @staticmethod
    def pixel_for_direction(angle: int | float, *, offset: int | float = 0) -> int:
        """Map a clockwise angle to the nearest of the 12 ring pixels."""
        if not 0 <= angle < 360:
            raise ValueError("Direction angle must be in the range 0 <= angle < 360.")
        normalized = (angle + offset) % 360
        return int((normalized + 15) / (360 / PIXEL_COUNT)) % PIXEL_COUNT

    def show_direction(
        self,
        angle: int | float,
        *,
        offset: int | float = 0,
        background: int = 0x001010,
        accent: int = 0x0080FF,
        neighbor: int = 0x003060,
    ) -> int:
        """Show a three-pixel wedge pointing toward the measured direction."""
        pixel = self.pixel_for_direction(angle, offset=offset)
        colors = [background] * PIXEL_COUNT
        colors[(pixel - 1) % PIXEL_COUNT] = neighbor
        colors[pixel] = accent
        colors[(pixel + 1) % PIXEL_COUNT] = neighbor
        self.show_pixels(colors)
        return pixel

    def off(self) -> None:
        if not self._enabled:
            return
        try:
            self.set_color(0)
        except Exception:
            self._enabled = False

    def __call__(self, event: StateEvent) -> None:
        if not self._enabled:
            return
        try:
            if event.state is AgentState.LISTENING:
                self.listen_for_direction()
            else:
                self.set_color(STATE_COLORS[event.state])
        except Exception:
            self._enabled = False
            raise

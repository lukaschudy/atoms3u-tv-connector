"""Minimal read-only access to ReSpeaker voice activity and direction data."""

from __future__ import annotations

from collections.abc import Callable
import struct
from typing import Any

from respeaker_led import USB_PRODUCT_ID, USB_TIMEOUT_MS, USB_VENDOR_ID


USB_CONTROL_IN_VENDOR_DEVICE = 0xC0
DOA_PARAMETER_ID = 21
DOA_OFFSET = 0
VOICE_ACTIVITY_PARAMETER_ID = 19
VOICE_ACTIVITY_OFFSET = 32


class TuningUnavailable(RuntimeError):
    pass


class ReSpeakerTuning:
    """Read the firmware's processed direction-of-arrival information."""

    def __init__(
        self,
        device: Any | None = None,
        *,
        finder: Callable[..., Any] | None = None,
    ) -> None:
        if device is None:
            if finder is None:
                try:
                    import usb.core
                except ImportError as exc:
                    raise TuningUnavailable(
                        "PyUSB is not installed; install the Debian package python3-usb."
                    ) from exc
                finder = usb.core.find
            try:
                device = finder(idVendor=USB_VENDOR_ID, idProduct=USB_PRODUCT_ID)
            except Exception as exc:
                raise TuningUnavailable(
                    f"Cannot search for the ReSpeaker USB interface: {exc}"
                ) from exc

        if device is None:
            raise TuningUnavailable(
                "ReSpeaker USB 4 Mic Array tuning interface 2886:0018 was not found."
            )
        self._device = device

    def read_int(self, parameter_id: int, offset: int) -> int:
        if not 0 <= parameter_id <= 0xFFFF:
            raise ValueError("Parameter ID must fit in 16 bits.")
        if not 0 <= offset <= 0x3F:
            raise ValueError("Integer parameter offset must be between 0 and 63.")

        command = 0x80 | 0x40 | offset
        try:
            response = self._device.ctrl_transfer(
                USB_CONTROL_IN_VENDOR_DEVICE,
                0,
                command,
                parameter_id,
                8,
                USB_TIMEOUT_MS,
            )
        except Exception as exc:
            raise TuningUnavailable(f"Cannot read ReSpeaker tuning data: {exc}") from exc

        raw = bytes(response)
        if len(raw) != 8:
            raise TuningUnavailable(
                f"ReSpeaker returned {len(raw)} bytes; expected 8."
            )
        value, _maximum = struct.unpack("<ii", raw)
        return value

    @property
    def direction(self) -> int:
        angle = self.read_int(DOA_PARAMETER_ID, DOA_OFFSET)
        if not 0 <= angle < 360:
            raise TuningUnavailable(f"ReSpeaker returned invalid direction {angle}.")
        return angle

    @property
    def voice_activity(self) -> bool:
        return bool(
            self.read_int(VOICE_ACTIVITY_PARAMETER_ID, VOICE_ACTIVITY_OFFSET)
        )

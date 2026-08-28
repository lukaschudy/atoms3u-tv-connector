"""Physically validate ReSpeaker voice direction and its 12-pixel LED ring."""

from __future__ import annotations

import argparse
import os
import subprocess
import time

from respeaker_led import LedUnavailable, ReSpeakerLedObserver, STATE_COLORS
from respeaker_tuning import ReSpeakerTuning, TuningUnavailable
from agent_state import AgentState


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Point the ReSpeaker LED ring toward the active speaker."
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=20,
        help="How long to run the physical test (default: 20).",
    )
    parser.add_argument(
        "--offset",
        type=float,
        default=0,
        help="Clockwise LED calibration offset in degrees (default: 0).",
    )
    parser.add_argument(
        "--brightness",
        type=int,
        default=10,
        help="LED brightness from 0 to 31 (default: 10).",
    )
    parser.add_argument(
        "--always-show",
        action="store_true",
        help="Display the last direction even when voice activity is false.",
    )
    parser.add_argument(
        "--audio-device",
        default="plughw:CARD=ArrayUAC10,DEV=0",
        help="ALSA capture device used to activate the ReSpeaker DSP.",
    )
    return parser.parse_args()


def start_audio_stream(audio_device: str) -> subprocess.Popen[str]:
    """Open a discarded six-channel stream so the ReSpeaker DSP updates DOA."""
    try:
        process = subprocess.Popen(
            [
                "arecord",
                "-q",
                "-D",
                audio_device,
                "-f",
                "S16_LE",
                "-r",
                "16000",
                "-c",
                "6",
                "-t",
                "raw",
                os.devnull,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TuningUnavailable("arecord is not installed.") from exc

    time.sleep(0.2)
    if process.poll() is not None:
        detail = (process.stderr.read() if process.stderr else "").strip()
        raise TuningUnavailable(f"Cannot open the ReSpeaker audio stream: {detail}")
    return process


def stop_audio_stream(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def main() -> int:
    args = parse_args()
    if args.seconds <= 0:
        raise SystemExit("--seconds must be greater than zero.")

    try:
        led = ReSpeakerLedObserver(brightness=args.brightness)
        tuning = ReSpeakerTuning()
        audio_stream = start_audio_stream(args.audio_device)
    except (LedUnavailable, TuningUnavailable) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    print(
        "Direction test ready. Speak from the front, right, back and left "
        f"for {args.seconds:g} seconds."
    )
    deadline = time.monotonic() + args.seconds
    previous_activity: bool | None = None
    previous_pixel: int | None = None

    try:
        while time.monotonic() < deadline:
            active = tuning.voice_activity
            if active or args.always_show:
                angle = tuning.direction
                pixel = led.show_direction(angle, offset=args.offset)
                if active != previous_activity or pixel != previous_pixel:
                    print(
                        f"VOICE={'yes' if active else 'no '}  "
                        f"ANGLE={angle:3d} deg  LED={pixel:02d}",
                        flush=True,
                    )
                previous_pixel = pixel
            elif active != previous_activity:
                led.set_color(STATE_COLORS[AgentState.READY])
                print("VOICE=no   waiting...", flush=True)

            previous_activity = active
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("Stopped.")
    except TuningUnavailable as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        stop_audio_stream(audio_stream)
        led.set_color(STATE_COLORS[AgentState.READY])

    print("Direction test complete; LEDs returned to READY.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

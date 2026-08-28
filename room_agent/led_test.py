"""Cycle through room-agent LED states without starting audio capture."""

from __future__ import annotations

import argparse
import sys
import time

from agent_state import AgentState, StateEvent
from respeaker_led import LedUnavailable, ReSpeakerLedObserver


TEST_STATES = (
    AgentState.READY,
    AgentState.LISTENING,
    AgentState.TRANSCRIBING,
    AgentState.ROUTING,
    AgentState.EXECUTING,
    AgentState.SUCCESS,
    AgentState.ERROR,
    AgentState.READY,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brightness", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=1.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.seconds < 0.1 or args.seconds > 10:
        print("ERROR: --seconds must be between 0.1 and 10.", file=sys.stderr)
        return 2

    try:
        led = ReSpeakerLedObserver(brightness=args.brightness)
    except (LedUnavailable, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    previous = AgentState.STARTING
    try:
        for sequence, state in enumerate(TEST_STATES, start=1):
            print(f"LED: {state.value}")
            if state is AgentState.LISTENING:
                print("  Speak and move around the microphone; the ring should point at you.")
            led(
                StateEvent(
                    sequence=sequence,
                    previous=previous,
                    state=state,
                    occurred_at=time.time(),
                    detail="LED hardware test",
                )
            )
            previous = state
            time.sleep(args.seconds)
    except KeyboardInterrupt:
        led.off()
        print("LED test cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: LED control failed: {exc}", file=sys.stderr)
        return 2

    print("LED test complete; leaving the ring in READY cyan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

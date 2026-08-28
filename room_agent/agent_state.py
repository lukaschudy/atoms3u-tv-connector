"""Observable state machine for the Raspberry Pi room-agent lifecycle."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum


class AgentState(str, Enum):
    STARTING = "STARTING"
    READY = "READY"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    ROUTING = "ROUTING"
    EXECUTING = "EXECUTING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


ALLOWED_TRANSITIONS = {
    AgentState.STARTING: {AgentState.READY, AgentState.ERROR, AgentState.STOPPED},
    AgentState.READY: {
        AgentState.LISTENING,
        AgentState.ROUTING,
        AgentState.ERROR,
        AgentState.STOPPED,
    },
    AgentState.LISTENING: {
        AgentState.TRANSCRIBING,
        AgentState.ERROR,
        AgentState.STOPPED,
    },
    AgentState.TRANSCRIBING: {
        AgentState.ROUTING,
        AgentState.ERROR,
        AgentState.STOPPED,
    },
    AgentState.ROUTING: {
        AgentState.EXECUTING,
        AgentState.READY,
        AgentState.ERROR,
        AgentState.STOPPED,
    },
    AgentState.EXECUTING: {
        AgentState.SUCCESS,
        AgentState.ERROR,
        AgentState.STOPPED,
    },
    AgentState.SUCCESS: {AgentState.READY, AgentState.STOPPED},
    AgentState.ERROR: {AgentState.READY, AgentState.STOPPED},
    AgentState.STOPPED: set(),
}


class InvalidStateTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class StateEvent:
    sequence: int
    previous: AgentState
    state: AgentState
    occurred_at: float
    detail: str = ""


StateObserver = Callable[[StateEvent], None]
ObserverErrorHandler = Callable[[StateObserver, Exception], None]


class StateMachine:
    """Validate lifecycle transitions and notify non-critical observers."""

    def __init__(
        self,
        observers: Iterable[StateObserver] = (),
        *,
        clock: Callable[[], float] = time.time,
        on_observer_error: ObserverErrorHandler | None = None,
    ) -> None:
        self._state = AgentState.STARTING
        self._sequence = 0
        self._observers = list(observers)
        self._clock = clock
        self._on_observer_error = on_observer_error

    @property
    def state(self) -> AgentState:
        return self._state

    def subscribe(self, observer: StateObserver) -> None:
        self._observers.append(observer)

    def transition(self, new_state: AgentState, detail: str = "") -> StateEvent:
        if new_state not in ALLOWED_TRANSITIONS[self._state]:
            raise InvalidStateTransition(
                f"State cannot change from {self._state.value} to {new_state.value}."
            )

        previous = self._state
        self._state = new_state
        self._sequence += 1
        event = StateEvent(
            sequence=self._sequence,
            previous=previous,
            state=new_state,
            occurred_at=self._clock(),
            detail=detail,
        )
        for observer in tuple(self._observers):
            try:
                observer(event)
            except Exception as exc:  # Observers must never break command execution.
                if self._on_observer_error is not None:
                    try:
                        self._on_observer_error(observer, exc)
                    except Exception:
                        pass
        return event

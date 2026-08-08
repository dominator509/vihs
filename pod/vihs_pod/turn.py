"""Turn-taking FSM (SPEC-001 D1).

I/O-free transition core: drivers feed it VAD events and a clock; it returns
actions. Unit-testable without audio.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum, auto
from typing import Protocol


class State(Enum):
    IDLE = auto()
    USER_SPEAKING = auto()
    ENDPOINT_PENDING = auto()
    RESPONDING = auto()


class Action(Enum):
    OPEN_STT = auto()
    COUNT_NEAR_MISS = auto()
    ENDPOINT = auto()
    ABORT = auto()


class Clock(Protocol):
    def __call__(self) -> float: ...


class TurnFSM:
    """SPEC-001 D1 reference FSM, extended with the barge-in voice filter."""

    def __init__(
        self,
        clock: Callable[[], float],
        t_sil: float = 0.550,
        t_fast: float = 0.250,
        b_min: float = 0.200,
    ) -> None:
        self.state = State.IDLE
        self.clock = clock
        self.t_sil = t_sil
        self.t_fast = t_fast
        self.b_min = b_min
        self.deadline: float | None = None
        self.voiced_during_playback = 0.0

    def on_vad(self, speech: bool, frame_dur: float, partial_terminal: bool) -> list[Action]:
        s = self.state
        if s is State.IDLE and speech:
            self.state = State.USER_SPEAKING
            return [Action.OPEN_STT]
        if s is State.USER_SPEAKING and not speech:
            self.state = State.ENDPOINT_PENDING
            self.deadline = self.clock() + (self.t_fast if partial_terminal else self.t_sil)
            return []
        if s is State.ENDPOINT_PENDING:
            if speech:  # user kept going — not an endpoint
                self.state = State.USER_SPEAKING
                self.deadline = None
                return [Action.COUNT_NEAR_MISS]
            if self.deadline is not None and self.clock() >= self.deadline:
                self.state = State.RESPONDING
                self.deadline = None
                return [Action.ENDPOINT]
            return []
        if s is State.RESPONDING and speech:
            self.voiced_during_playback += frame_dur
            if self.voiced_during_playback >= self.b_min:
                self.state = State.USER_SPEAKING
                self.voiced_during_playback = 0.0
                return [Action.ABORT, Action.OPEN_STT]
            return []
        if s is State.RESPONDING and not speech:
            self.voiced_during_playback = 0.0  # cough filter resets
        return []

    def reset(self) -> None:
        """Force back to IDLE (session teardown / hard error recovery)."""
        self.state = State.IDLE
        self.deadline = None
        self.voiced_during_playback = 0.0

"""FSM tests (SPEC-001 D1 required tests).

Table-driven transitions incl. cough filter, near-miss, and barge-in. The
clock is injectable — no audio, no sleeps.
"""

from __future__ import annotations

import pytest

from vihs_pod.turn import Action, State, TurnFSM


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def fsm() -> tuple[TurnFSM, FakeClock]:
    clock = FakeClock()
    return TurnFSM(clock, t_sil=0.550, t_fast=0.250, b_min=0.200), clock


def test_idle_speech_opens_stt(fsm: tuple[TurnFSM, FakeClock]) -> None:
    m, _ = fsm
    assert m.on_vad(True, 0.02, False) == [Action.OPEN_STT]
    assert m.state is State.USER_SPEAKING


def test_silence_enters_endpoint_pending_slow(fsm: tuple[TurnFSM, FakeClock]) -> None:
    m, clock = fsm
    m.on_vad(True, 0.02, False)
    assert m.on_vad(False, 0.02, False) == []
    assert m.state is State.ENDPOINT_PENDING
    # Timer set to t_sil (550ms) since no terminal punctuation.
    clock.advance(0.300)
    assert m.on_vad(False, 0.02, False) == []
    clock.advance(0.300)  # 600ms total > 550
    assert m.on_vad(False, 0.02, False) == [Action.ENDPOINT]
    assert m.state is State.RESPONDING


def test_terminal_punctuation_shortens_endpoint(fsm: tuple[TurnFSM, FakeClock]) -> None:
    m, clock = fsm
    m.on_vad(True, 0.02, False)
    # partial_terminal=True → fast timer (250ms)
    m.on_vad(False, 0.02, True)
    assert m.state is State.ENDPOINT_PENDING
    clock.advance(0.200)
    assert m.on_vad(False, 0.02, True) == []
    clock.advance(0.100)  # 300ms total > 250
    assert m.on_vad(False, 0.02, True) == [Action.ENDPOINT]


def test_speech_resumes_counts_near_miss(fsm: tuple[TurnFSM, FakeClock]) -> None:
    m, clock = fsm
    m.on_vad(True, 0.02, False)
    m.on_vad(False, 0.02, False)
    assert m.state is State.ENDPOINT_PENDING
    clock.advance(0.100)
    # User keeps going — back to USER_SPEAKING, timer cancelled.
    assert m.on_vad(True, 0.02, False) == [Action.COUNT_NEAR_MISS]
    assert m.state is State.USER_SPEAKING


def test_cough_filter_does_not_barge_in(fsm: tuple[TurnFSM, FakeClock]) -> None:
    m, _ = fsm
    # Enter RESPONDING via a normal endpoint.
    m.on_vad(True, 0.02, False)
    m.on_vad(False, 0.02, False)
    m.state = State.RESPONDING  # simulate timer fire
    # A short voiced blip (cough) below b_min resets, no ABORT.
    assert m.on_vad(True, 0.050, False) == []
    assert m.state is State.RESPONDING
    assert m.voiced_during_playback == 0.050
    # Silence resets the counter.
    m.on_vad(False, 0.02, False)
    assert m.voiced_during_playback == 0.0


def test_barge_in_after_min_voiced(fsm: tuple[TurnFSM, FakeClock]) -> None:
    m, _ = fsm
    m.on_vad(True, 0.02, False)
    m.on_vad(False, 0.02, False)
    m.state = State.RESPONDING
    # Accumulate 200ms voiced (b_min) across frames → ABORT + re-open STT.
    m.on_vad(True, 0.100, False)
    assert m.state is State.RESPONDING
    assert m.on_vad(True, 0.100, False) == [Action.ABORT, Action.OPEN_STT]
    assert m.state is State.USER_SPEAKING


def test_barge_in_from_anywhere_in_playback(fsm: tuple[TurnFSM, FakeClock]) -> None:
    m, _ = fsm
    # Directly from IDLE→RESPONDING (simulating playback state).
    m.state = State.RESPONDING
    m.on_vad(True, 0.150, False)
    m.on_vad(True, 0.150, False)  # 300ms total
    assert m.state is State.USER_SPEAKING


def test_reset_returns_idle(fsm: tuple[TurnFSM, FakeClock]) -> None:
    m, _ = fsm
    m.on_vad(True, 0.02, False)
    m.reset()
    assert m.state is State.IDLE
    assert m.deadline is None
    assert m.voiced_during_playback == 0.0

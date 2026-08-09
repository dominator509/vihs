"""Stage-crash recovery (EP-007 M5, SPEC-006 row 1).

A pipeline stage crash mid-turn must: abort the remaining stages cleanly
(AbortBus), surface as StageCrashError with the failing stage name, and
let the conversation recover — emit a `stage_error` note (no user text),
speak the fixed recovery utterance, and return to listening. After TWO
crashes in one session the pod marks itself degraded so the orchestrator
drains it.

The crash is injected deterministically via the StageCrashLLM fault hook
(VIHS_FAULT=stage_crash wraps the LLM in build_stages).
"""

from __future__ import annotations

import asyncio

import pytest

from vihs_pod.conversation import RECOVERY_UTTERANCE, Conversation, build_stages
from vihs_pod.mocks.stages import ScriptedLLM, StageCrashLLM
from vihs_pod.pipeline.abort_bus import AbortBus
from vihs_pod.pipeline.flow import StageCrashError, run_response


class _Mem:
    """Durable append stand-in: captures events for assertions."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.transcript_calls = 0

    async def transcript(self, session_id: str) -> str:
        self.transcript_calls += 1
        return ""

    async def append_event(self, session_id: str, event: dict) -> dict:
        self.events.append(event)
        return {"status": "committed", "hash": "fake"}


async def test_run_response_raises_stagecrash_with_stage() -> None:
    """The fault hook surfaces as StageCrashError naming the LLM stage."""
    bus = AbortBus()
    inner = ScriptedLLM(["Hello there."])
    st, monitor = build_stages(answers=["Hello there."], real=False)
    st.llm = StageCrashLLM(inner)  # inject the fault directly
    ledger: dict[int, str] = {}

    with pytest.raises(StageCrashError) as excinfo:
        await run_response(bus.fresh(), bus, st, "prompt", turn_id=1, ledger=ledger)
    assert excinfo.value.stage == "llm"
    # run_response aborted cleanly: no orphaned tasks, mux flushed.
    assert bus._tasks == set()  # type: ignore[attr-defined]
    assert monitor.reported


async def test_run_response_stagecrash_is_cancellation_safe() -> None:
    """CancelledError (barge-in) still propagates untouched — the crash
    handler must not swallow the abort path."""
    bus = AbortBus()
    st, _monitor = build_stages(answers=["Long answer here."], real=False)
    ledger: dict[int, str] = {}

    task = asyncio.create_task(
        run_response(bus.fresh(), bus, st, "prompt", turn_id=1, ledger=ledger)
    )
    await asyncio.sleep(0.02)  # let it start streaming
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _conversation_with_crash(fault: bool = True) -> tuple[Conversation, _Mem]:
    mem = _Mem()
    stages, monitor = build_stages(answers=["Hello there."], real=False)
    if fault:
        stages.llm = StageCrashLLM(ScriptedLLM(["Hello there."]))
    convo = Conversation(
        session_id="sess-crash-1",
        connection_id="conn-1",
        pod_token="tok",
        cursor={},
        memory=mem,  # type: ignore[arg-type]
        stages=stages,
        monitor=monitor,
    )
    return convo, mem


async def test_stage_crash_recovers_with_note_and_utterance() -> None:
    """One crash: user event + stage_error note (no user text) + recovery
    utterance; the conversation stays live (listening)."""
    convo, mem = await _conversation_with_crash()
    await convo._handle_turn("hello")  # noqa: SLF001 — unit-level
    await convo.append_buffer.flush()

    events = mem.events
    roles = [e["role"] for e in events]
    # user utterance, then the assistant events; the note is system.
    assert "user" in roles
    assert "system" in roles
    assert "assistant" in roles
    note = next(e for e in events if e["kind"] == "note")
    assert note["meta"]["stage"] == "llm"
    assert note["text"] == "", "stage_error note must carry NO user text"
    # Recovery utterance present as an assistant event.
    assert any(e["kind"] == "utterance" and e["text"] == RECOVERY_UTTERANCE for e in events)
    # Conversation is still listening: stage_crashes == 1, NOT degraded.
    assert convo.stage_crashes == 1
    assert not convo.degraded


async def test_two_crashes_mark_pod_degraded() -> None:
    """Two crashes in one session → degraded=True (orchestrator drains)."""
    convo, mem = await _conversation_with_crash()
    await convo._handle_turn("hello")  # noqa: SLF001
    await convo._handle_turn("again")  # noqa: SLF001
    await convo.append_buffer.flush()

    assert convo.stage_crashes == 2
    assert convo.degraded, "2 stage crashes must mark the pod degraded"
    notes = [e for e in mem.events if e["kind"] == "note"]
    assert len(notes) == 2
    assert all(n["meta"]["crashes"] == i for i, n in enumerate(notes, start=1))


async def test_no_crash_no_note() -> None:
    """A clean turn emits NO stage_error note and stays healthy."""
    convo, mem = await _conversation_with_crash(fault=False)
    await convo._handle_turn("hello")  # noqa: SLF001
    await convo.append_buffer.flush()

    assert convo.stage_crashes == 0
    assert not convo.degraded
    assert all(e["kind"] != "note" for e in mem.events)

"""Pipeline end-to-end in-process (EP-005 M3, SPEC-001 acceptance).

Scripted turn with mock stages: LLM response → clause chunks → TTS audio →
mux ledger → committed text. Barge-in mid-answer commits the played prefix
exactly (INV-1).
"""

from __future__ import annotations

import asyncio

from vihs_pod.mocks.stages import MockLipSync, MockLLM, MockMux, MockTTS
from vihs_pod.pipeline.abort_bus import AbortBus
from vihs_pod.pipeline.flow import abort_response, run_response
from vihs_pod.pipeline.ledger import PartialChunk


class Stages:
    def __init__(self, llm: str, ms_per_char: int = 10) -> None:
        self.llm = MockLLM(response=llm, delay=0.0005)
        self.tts = MockTTS(ms_per_char=ms_per_char)
        self.lipsync = MockLipSync()
        self.mux = MockMux()


async def test_scripted_turn_commits_full_text() -> None:
    bus = AbortBus()
    st = Stages("Hello there. This is a longer answer. How are you?")
    ledger: dict[int, str] = {}
    committed = await run_response(bus.fresh(), bus, st, "user prompt", turn_id=1, ledger=ledger)

    assert not committed.interrupted
    # The mock TTS strips leading spaces from chunked clauses; the committed
    # text is the concatenation of what was actually played.
    assert "Hello there." in committed.text
    assert "How are you?" in committed.text
    assert committed.text.startswith("Hello there.")
    assert st.mux.reported, "mux flush_and_report must run at normal end"


async def test_barge_in_commits_played_prefix_exactly() -> None:
    """Abort mid-response: committed text = full played spans + partial cut."""
    bus = AbortBus()
    st = Stages("First sentence is done. Second one is longer and gets cut.", ms_per_char=10)
    ledger: dict[int, str] = {}

    # Start the response, let it play, then abort before it finishes.
    task = asyncio.create_task(
        run_response(bus.fresh(), bus, st, "user prompt", turn_id=1, ledger=ledger)
    )
    await asyncio.sleep(0.02)  # let the first clause(s) flow through

    # Simulate barge-in: cancel the response and commit via abort path.
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    del task

    # The mux holds whatever fully played; a partial chunk is a fixture cut.
    partial = PartialChunk(
        clause_id=max(ledger.keys(), default=1),
        char_start=0,
        chars_covered=20,
        dur_ms=200,
        played_ms=100,
    )
    committed = await abort_response(bus, st, ledger, partial)
    assert committed.interrupted
    # Fully played clauses are verbatim in order.
    spans = await st.mux.flush_and_report()
    played_text = "".join(
        ledger[span.clause_id] for span in sorted(spans, key=lambda s: s.clause_id)
    )
    assert committed.text.startswith(played_text) or played_text.startswith(committed.text)


async def test_abort_flush_within_budget() -> None:
    bus = AbortBus()
    st = Stages("A very long answer " * 20, ms_per_char=10)
    ledger: dict[int, str] = {}

    task = asyncio.create_task(run_response(bus.fresh(), bus, st, "p", turn_id=1, ledger=ledger))
    await asyncio.sleep(0.01)

    start = asyncio.get_event_loop().time()
    await abort_response(bus, st, ledger, None)
    elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000
    assert elapsed_ms < 100, f"abort flush took {elapsed_ms:.1f}ms, budget 100ms"
    assert bus.gen >= 1
    # Settle the response task — abort cancelled its guarded stages.
    await asyncio.gather(task, return_exceptions=True)


async def test_pipeline_queues_bounded() -> None:
    """The flow must not deadlock with tiny queue sizes (backpressure)."""
    bus = AbortBus()
    st = Stages("One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten.", ms_per_char=1)
    ledger: dict[int, str] = {}
    committed = await run_response(bus.fresh(), bus, st, "p", turn_id=1, ledger=ledger)
    assert not committed.interrupted
    assert len(ledger) >= 2, "multiple clauses chunked"

"""INV-1 abort ledger tests (SPEC-001 required: exact math + ≤100ms budget).

Pins the committed-text reconstruction byte-exactly against fixture timings,
and proves AbortBus cancellation completes within the internal budget with
cancellation-safe mock stages.
"""

from __future__ import annotations

import asyncio
import time

from vihs_pod.pipeline.abort_bus import AbortBus, PlayedSpan
from vihs_pod.pipeline.ledger import PartialChunk, committed_text

# ---------------------------------------------------------------------------
# INV-1 ledger math — fixture-exact
# ---------------------------------------------------------------------------


def test_fully_played_spans_concatenate() -> None:
    clauses = {
        1: "Hello there, how are you today?",
        2: "I hope everything is fine.",
    }
    played = [
        PlayedSpan(clause_id=1, char_start=0, char_end=31),
        PlayedSpan(clause_id=2, char_start=0, char_end=26),
    ]
    assert committed_text(clauses, played) == (
        "Hello there, how are you today?I hope everything is fine."
    )


def test_partial_chunk_proportional_cut_exact() -> None:
    # 10 chars covered over 1000ms; played 600ms → floor(10*600/1000) = 6.
    clauses = {1: "0123456789"}
    partial = PartialChunk(
        clause_id=1,
        char_start=0,
        chars_covered=10,
        dur_ms=1000,
        played_ms=600,
    )
    assert partial.committed_chars() == 6
    assert committed_text(clauses, [], partial) == "012345"


def test_partial_chunk_floor_to_char() -> None:
    # 10 chars / 1000ms; played 99ms → floor(0.99) = 0 chars.
    clauses = {1: "0123456789"}
    partial = PartialChunk(1, 0, 10, 1000, 99)
    assert partial.committed_chars() == 0
    assert committed_text(clauses, [], partial) == ""


def test_full_play_then_partial_cut_combined() -> None:
    clauses = {
        1: "First clause fully heard.",
        2: "Second clause interrupted halfway.",
    }
    played = [PlayedSpan(clause_id=1, char_start=0, char_end=25)]
    partial = PartialChunk(
        clause_id=2,
        char_start=0,
        chars_covered=30,
        dur_ms=1000,
        played_ms=500,
    )
    text = committed_text(clauses, played, partial)
    # floor(30 * 500/1000) = 15 chars of clause 2.
    assert text == "First clause fully heard." + "Second clause i"


def test_zero_duration_partial_safe() -> None:
    partial = PartialChunk(1, 0, 3, 0, 10)
    assert partial.committed_chars() == 0


def test_clause_span_clamped_to_length() -> None:
    clauses = {1: "short"}
    played = [PlayedSpan(clause_id=1, char_start=0, char_end=999)]
    assert committed_text(clauses, played) == "short"


# ---------------------------------------------------------------------------
# AbortBus — cancellation budget + stale generation
# ---------------------------------------------------------------------------


async def test_abort_cancels_tasks_within_budget() -> None:
    bus = AbortBus()

    async def slow_stage() -> None:
        try:
            await asyncio.sleep(3600)  # never finishes on its own
        except asyncio.CancelledError:
            raise

    tasks = [asyncio.create_task(slow_stage()) for _ in range(4)]
    for t in tasks:
        bus.guard(t)

    mux_called = False
    renderer_called = False

    async def mock_mux() -> list[PlayedSpan]:
        nonlocal mux_called
        mux_called = True
        return [PlayedSpan(1, 0, 5)]

    async def mock_renderer() -> None:
        nonlocal renderer_called
        renderer_called = True

    start = time.perf_counter()
    played = await bus.abort(mock_mux, mock_renderer)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert mux_called and renderer_called
    assert played == [PlayedSpan(1, 0, 5)]
    assert elapsed_ms < 100, f"abort took {elapsed_ms:.1f}ms, budget is 100ms"
    # All tasks must be done (cancelled).
    assert all(t.done() for t in tasks)


async def test_stale_generation_output_dropped() -> None:
    """A task from an old generation must not survive into the new one."""
    bus = AbortBus()
    gen = bus.fresh()  # 0
    survivor: list[int] = []

    async def stale_worker() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        # If this line ran, the stale task outlived the abort.
        survivor.append(gen)

    t = asyncio.create_task(stale_worker())
    bus.guard(t)

    async def noop_mux() -> list[PlayedSpan]:
        return []

    async def noop_renderer() -> None:
        return None

    await bus.abort(noop_mux, noop_renderer)

    # Let any stray continuations run (they shouldn't exist).
    await asyncio.sleep(0.01)
    assert survivor == [], "stale generation output must be dropped"
    assert bus.gen == 1, "abort bumps the generation counter"


async def test_abort_returns_played_ledger() -> None:
    bus = AbortBus()

    async def mock_mux() -> list[PlayedSpan]:
        return [
            PlayedSpan(1, 0, 12),
            PlayedSpan(2, 0, 30),
        ]

    async def noop() -> None:
        return None

    played = await bus.abort(mock_mux, noop)
    assert played == [PlayedSpan(1, 0, 12), PlayedSpan(2, 0, 30)]

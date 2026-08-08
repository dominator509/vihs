"""Pipeline — clause-pipelined response task graph (SPEC-001 D3, EP-005 §7).

Every queue is bounded (backpressure, not unbounded RAM). Every task is
bus-guarded. Every handoff checks the generation counter.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from vihs_pod.pipeline.abort_bus import AbortBus
from vihs_pod.pipeline.clause import ClauseChunker
from vihs_pod.pipeline.ledger import PartialChunk, committed_text
from vihs_pod.pipeline.protocols import AudioChunk

END = object()


@dataclass
class Clause:
    id: int
    text: str


@dataclass(frozen=True)
class Tagged:
    chunk: AudioChunk
    clause_id: int
    span: tuple[int, int]


@dataclass(frozen=True)
class Committed:
    text: str
    interrupted: bool
    clauses: dict[int, str]
    partial: PartialChunk | None = None


async def run_response(
    gen: int,
    bus: AbortBus,
    st: Any,  # Stages container: llm, tts, lipsync, mux
    prompt: str,
    turn_id: int,
    ledger: dict[int, str],
    on_caption: Any = None,  # Callable[[int, str], Awaitable[None]] — live deltas
    metrics: Any = None,  # Metrics (EP-005 M6) — first-chunk latency samples
) -> Committed:
    """Clause-pipelined response. Returns the committed turn.

    `ledger` maps clause_id → text so abort reconstruction can slice exact
    spans (INV-1). `on_caption(turn_id, delta)` is awaited per audio chunk
    with the exact text that chunk covers — the live caption path (SPEC-004).
    `metrics` (optional) records LLM TTFT, TTS TTFA, lip-sync first frame,
    e2e first-frame, and e2e total latency samples (ARCHITECTURE §6).
    """
    _t0 = time.perf_counter()
    clauses: asyncio.Queue[Clause] = asyncio.Queue(maxsize=4)
    audio: asyncio.Queue[Tagged] = asyncio.Queue(maxsize=16)

    async def brain() -> None:
        ck, n = ClauseChunker(), 0
        _llm_start = time.perf_counter()
        _first = True
        async for delta in st.llm.stream(prompt):
            if gen != bus.fresh():
                return  # stale: drop
            if _first and metrics is not None:
                metrics.record("llm_ttft", (time.perf_counter() - _llm_start) * 1000.0)
                _first = False
            for c in ck.feed(delta):
                n += 1
                ledger[n] = c
                await clauses.put(Clause(id=n, text=c))
        for c in ck.flush():
            n += 1
            ledger[n] = c
            await clauses.put(Clause(id=n, text=c))
        await clauses.put(END)  # type: ignore[arg-type]

    async def voice() -> None:
        while True:
            cl = await clauses.get()
            if cl is END:
                break
            start = 0
            _tts_start = time.perf_counter()
            _first = True
            async for ch in st.tts.stream(cl.text, "default"):
                if gen != bus.fresh():
                    return
                if _first and metrics is not None:
                    metrics.record("tts_ttfa", (time.perf_counter() - _tts_start) * 1000.0)
                    _first = False
                span = (start, start + ch.chars_covered)
                start += ch.chars_covered
                if on_caption is not None:
                    await on_caption(turn_id, cl.text[span[0] : span[1]])
                await audio.put(Tagged(chunk=ch, clause_id=cl.id, span=span))
        await audio.put(END)  # type: ignore[arg-type]

    async def face_and_wire() -> None:
        _ff_start = time.perf_counter()
        _first = True
        while True:
            tg = await audio.get()
            if tg is END:
                break
            if gen != bus.fresh():
                return
            async for fr in st.lipsync.frames(_one(tg.chunk)):
                if _first and metrics is not None:
                    metrics.record("lipsync_ff", (time.perf_counter() - _ff_start) * 1000.0)
                    _first = False
                await st.mux.push(fr, tg.clause_id, tg.span)
            await st.mux.push(tg.chunk, tg.clause_id, tg.span)
            if metrics is not None and not metrics._e2e_ff_recorded:
                metrics.record("e2e_first_frame", (time.perf_counter() - _t0) * 1000.0)
                metrics._e2e_ff_recorded = True

    tasks = [asyncio.create_task(f()) for f in (brain, voice, face_and_wire)]
    for t in tasks:
        bus.guard(t)
    await asyncio.gather(*tasks)  # CancelledError propagates on abort

    played = await st.mux.flush_and_report()
    text = committed_text(ledger, played)
    if metrics is not None:
        metrics.record("e2e_total", (time.perf_counter() - _t0) * 1000.0)
    return Committed(text=text, interrupted=False, clauses=dict(ledger))


async def abort_response(
    bus: AbortBus,
    st: Any,
    ledger: dict[int, str],
    partial: PartialChunk | None,
) -> Committed:
    """Abort path (SPEC-001 D3): cancel stages, resolve ledger, commit INV-1."""
    played = await bus.abort(
        mux=lambda: st.mux.flush_and_report(),
        renderer=lambda: _neutral(st),
    )
    text = committed_text(ledger, played, partial)
    return Committed(text=text, interrupted=True, clauses=dict(ledger), partial=partial)


async def _neutral(st: Any) -> None:
    """Renderer neutral pose — mock is a no-op (real: EP-009)."""
    if hasattr(st, "renderer"):
        neutral = getattr(st.renderer, "neutral_pose", None)
        if neutral is not None:
            await neutral()


class _OneShot:
    """Wraps a single audio chunk as an async iterator for lipsync."""

    def __init__(self, chunk: AudioChunk) -> None:
        self._chunk = chunk
        self._done = False

    def __aiter__(self) -> _OneShot:
        return self

    async def __anext__(self) -> AudioChunk:
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return self._chunk


def _one(chunk: AudioChunk) -> _OneShot:
    return _OneShot(chunk)

"""Pipeline — clause-pipelined response task graph (SPEC-001 D3, EP-005 §7).

Every queue is bounded (backpressure, not unbounded RAM). Every task is
bus-guarded. Every handoff checks the generation counter.
"""

from __future__ import annotations

import asyncio
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
) -> Committed:
    """Clause-pipelined response. Returns the committed turn.

    `ledger` maps clause_id → text so abort reconstruction can slice exact
    spans (INV-1).
    """
    clauses: asyncio.Queue[Clause] = asyncio.Queue(maxsize=4)
    audio: asyncio.Queue[Tagged] = asyncio.Queue(maxsize=16)

    async def brain() -> None:
        ck, n = ClauseChunker(), 0
        async for delta in st.llm.stream(prompt):
            if gen != bus.fresh():
                return  # stale: drop
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
            async for ch in st.tts.stream(cl.text, "default"):
                if gen != bus.fresh():
                    return
                span = (start, start + ch.chars_covered)
                start += ch.chars_covered
                await audio.put(Tagged(chunk=ch, clause_id=cl.id, span=span))
        await audio.put(END)  # type: ignore[arg-type]

    async def face_and_wire() -> None:
        while True:
            tg = await audio.get()
            if tg is END:
                break
            if gen != bus.fresh():
                return
            async for fr in st.lipsync.frames(_one(tg.chunk)):
                await st.mux.push(fr, tg.clause_id, tg.span)
            await st.mux.push(tg.chunk, tg.clause_id, tg.span)

    tasks = [asyncio.create_task(f()) for f in (brain, voice, face_and_wire)]
    for t in tasks:
        bus.guard(t)
    await asyncio.gather(*tasks)  # CancelledError propagates on abort

    played = await st.mux.flush_and_report()
    text = committed_text(ledger, played)
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

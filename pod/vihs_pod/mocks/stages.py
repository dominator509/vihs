"""Deterministic mock stages (CI-safe, EP-005 M3).

Each mock emits scripted output with NO external calls, so pipeline tests
assert exact end-to-end behavior and budget numbers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from vihs_pod.pipeline.abort_bus import PlayedSpan
from vihs_pod.pipeline.protocols import AudioChunk, Frame


class MockLLM:
    """Emits a fixed scripted response one token at a time.

    `ttft` (seconds) simulates time-to-first-token for the M6 latency
    harness; default 0 keeps existing tests fast.
    """

    def __init__(
        self,
        response: str = "Hello there. This is a longer answer. How are you?",
        delay: float = 0.001,
        ttft: float = 0.0,
    ) -> None:
        self.response = response
        self.delay = delay
        self.ttft = ttft

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        # Emit character-by-character to exercise the chunker.
        if self.ttft > 0:
            await asyncio.sleep(self.ttft)
        for ch in self.response:
            await asyncio.sleep(self.delay)
            yield ch


class ScriptedLLM:
    """Emits the next scripted answer per stream() call (EP-005 M5).

    The E2E harness scripts the conversation: answer 1 is long (barge-in
    target), answer 2 acknowledges. Real adapters replace this in M7.
    `ttft` (seconds) simulates time-to-first-token (M6 latency harness).
    """

    def __init__(
        self,
        answers: list[str],
        fallback: str = "Understood.",
        delay: float = 0.001,
        ttft: float = 0.0,
    ) -> None:
        self._answers = list(answers)
        self._fallback = fallback
        self.delay = delay
        self.ttft = ttft

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        answer = self._answers.pop(0) if self._answers else self._fallback
        if self.ttft > 0:
            await asyncio.sleep(self.ttft)
        for ch in answer:
            await asyncio.sleep(self.delay)
            yield ch


class StageCrashLLM:
    """Fault hook (EP-007 M5, SPEC-006 row 1): raises after the FIRST
    token, simulating a pipeline stage crash mid-turn.

    Wraps a ScriptedLLM; the crash is deterministic (first stream() call
    raises) so the chaos drill and unit tests assert the exact recovery
    behavior: clean abort, `stage_error` note, recovery utterance, and
    degrade-after-2.
    """

    def __init__(self, inner: ScriptedLLM) -> None:
        self._inner = inner

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        async for ch in self._inner.stream(prompt):
            yield ch
            raise RuntimeError("fault: stage_crash (VIHS_FAULT injected)")


class MockTTS:
    """Converts a clause into audio chunks with known dur/chars coverage.

    One chunk per clause: chars_covered = len(clause), dur_ms proportional.
    `ttfa` (seconds) simulates time-to-first-audio for the M6 latency
    harness; default 0 keeps existing tests fast.
    """

    def __init__(self, ms_per_char: int = 10, ttfa: float = 0.0) -> None:
        self.ms_per_char = ms_per_char
        self.ttfa = ttfa

    async def stream(self, clause: str, voice: str) -> AsyncIterator[AudioChunk]:
        # Cover the RAW clause (whitespace included): the ledger slices the
        # chunker's exact clause text, so chars_covered must match its length
        # for INV-1 math to be byte-exact.
        n = len(clause)
        if n == 0:
            return
        if self.ttfa > 0:
            await asyncio.sleep(self.ttfa)
        pcm = b"\x00" * (n * 16)  # fake mono 16-bit samples
        yield AudioChunk(pcm=pcm, dur_ms=n * self.ms_per_char, chars_covered=n)


class MockLipSync:
    """Emits one timed frame per audio chunk.

    `ff` (seconds) simulates time-to-first-frame for the M6 latency
    harness; default 0 keeps existing tests fast.
    """

    def __init__(self, ff: float = 0.0) -> None:
        self.ff = ff

    async def frames(self, audio: AsyncIterator[AudioChunk]) -> AsyncIterator[Frame]:
        pts = 0
        first = True
        async for chunk in audio:
            if first and self.ff > 0:
                await asyncio.sleep(self.ff)
                first = False
            yield Frame(data=b"\x01" * 8, pts_ms=pts)
            pts += chunk.dur_ms


class MockMux:
    """Records pushed items and reports the playback ledger (INV-1)."""

    def __init__(self) -> None:
        self.items: list[tuple[object, int, tuple[int, int]]] = []
        self._reported = False

    async def push(self, item: object, clause_id: int, span: tuple[int, int]) -> None:
        if isinstance(item, AudioChunk):
            # Simulate on-wire playback duration FIRST: a chunk is only in the
            # playback ledger once its playback actually completed (INV-1).
            await asyncio.sleep(item.dur_ms / 1000.0)
        self.items.append((item, clause_id, span))

    async def flush_and_report(self) -> list[PlayedSpan]:
        # Every pushed audio chunk counts as fully played in the mock.
        self._reported = True
        spans: list[PlayedSpan] = []
        for item, clause_id, span in self.items:
            if isinstance(item, AudioChunk):
                spans.append(PlayedSpan(clause_id=clause_id, char_start=span[0], char_end=span[1]))
        return spans

    def reset(self) -> None:
        """Per-turn ledger reset (INV-1): one response per ledger."""
        self.items.clear()
        self._reported = False

    @property
    def reported(self) -> bool:
        return self._reported

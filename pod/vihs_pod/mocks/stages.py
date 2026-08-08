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
    """Emits a fixed scripted response one token at a time."""

    def __init__(
        self,
        response: str = "Hello there. This is a longer answer. How are you?",
        delay: float = 0.001,
    ) -> None:
        self.response = response
        self.delay = delay

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        # Emit character-by-character to exercise the chunker.
        for ch in self.response:
            await asyncio.sleep(self.delay)
            yield ch


class MockTTS:
    """Converts a clause into audio chunks with known dur/chars coverage.

    One chunk per clause: chars_covered = len(clause), dur_ms proportional.
    """

    def __init__(self, ms_per_char: int = 10) -> None:
        self.ms_per_char = ms_per_char

    async def stream(self, clause: str, voice: str) -> AsyncIterator[AudioChunk]:
        # Cover the RAW clause (whitespace included): the ledger slices the
        # chunker's exact clause text, so chars_covered must match its length
        # for INV-1 math to be byte-exact.
        n = len(clause)
        if n == 0:
            return
        pcm = b"\x00" * (n * 16)  # fake mono 16-bit samples
        yield AudioChunk(pcm=pcm, dur_ms=n * self.ms_per_char, chars_covered=n)


class MockLipSync:
    """Emits one timed frame per audio chunk."""

    async def frames(self, audio: AsyncIterator[AudioChunk]) -> AsyncIterator[Frame]:
        pts = 0
        async for chunk in audio:
            yield Frame(data=b"\x01" * 8, pts_ms=pts)
            pts += chunk.dur_ms


class MockMux:
    """Records pushed items and reports the playback ledger (INV-1)."""

    def __init__(self) -> None:
        self.items: list[tuple[object, int, tuple[int, int]]] = []
        self._reported = False

    async def push(self, item: object, clause_id: int, span: tuple[int, int]) -> None:
        self.items.append((item, clause_id, span))

    async def flush_and_report(self) -> list[PlayedSpan]:
        # Every pushed audio chunk counts as fully played in the mock.
        self._reported = True
        spans: list[PlayedSpan] = []
        for item, clause_id, span in self.items:
            if isinstance(item, AudioChunk):
                spans.append(PlayedSpan(clause_id=clause_id, char_start=span[0], char_end=span[1]))
        return spans

    @property
    def reported(self) -> bool:
        return self._reported

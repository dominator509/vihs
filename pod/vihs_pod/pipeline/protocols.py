"""Stage Protocols (EP-005 §7). Every §9 menu row swaps behind these.

Real adapters (EP-009) must satisfy the same contracts; mocks in CI are
deterministic so pipeline tests are exact.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from vihs_pod.pipeline.abort_bus import PlayedSpan


@dataclass(frozen=True)
class Partial:
    """Incremental STT result."""

    text: str
    final: bool = False


@dataclass(frozen=True)
class AudioChunk:
    """TTS output; ledger needs chars/ms."""

    pcm: bytes
    dur_ms: int
    chars_covered: int


@dataclass(frozen=True)
class Frame:
    """Renderer frame (lipsync stub)."""

    data: bytes
    pts_ms: int


class STT(Protocol):
    async def stream(self, pcm: AsyncIterator[bytes]) -> AsyncIterator[Partial]: ...


class LLM(Protocol):
    async def stream(self, prompt: str) -> AsyncIterator[str]: ...


class TTS(Protocol):
    async def stream(self, clause: str, voice: str) -> AsyncIterator[AudioChunk]: ...


class LipSync(Protocol):
    async def frames(self, audio: AsyncIterator[AudioChunk]) -> AsyncIterator[Frame]: ...


class Mux(Protocol):
    async def push(self, item: object, clause_id: int, span: tuple[int, int]) -> None: ...

    async def flush_and_report(self) -> list[PlayedSpan]: ...

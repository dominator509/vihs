"""Lip-sync stage adapter (EP-005 M7).

The stage Protocol is `frames(audio) -> AsyncIterator[Frame]` where each
`Frame(data, pts_ms)` renders one timed mouth pose. Real lip-sync models
land with hardware in EP-009 staging (plan §3 non-goal); this module
provides the adapter CONTRACT plus a frame-synthesizing stub that emits
one timed frame per audio chunk — deterministic, CI-safe, and exactly
the shape the real adapter will implement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from vihs_pod.pipeline.protocols import AudioChunk, Frame


class StubLipSync:
    """Emits one timed frame per audio chunk at the chunk's presentation time.

    Matches the MockLipSync shape so the pipeline treats real-mode and
    mock-mode identically at the contract boundary.
    """

    async def frames(self, audio: AsyncIterator[AudioChunk]) -> AsyncIterator[Frame]:
        pts = 0
        async for chunk in audio:
            yield Frame(data=b"\x01" * 8, pts_ms=pts)
            pts += chunk.dur_ms

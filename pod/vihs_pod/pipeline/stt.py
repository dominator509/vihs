"""Real STT stage adapter (EP-005 M7): faster-whisper streaming.

Config-gated: `faster_whisper` is imported lazily so CI (which has no
model weights) never pays for it. The adapter cancels the underlying
transcription work on generator close (SPEC-001 D3 — no orphaned GPU
work). Segments are emitted as they complete with `final=True`; a
sentinel ends the stream.
"""

from __future__ import annotations

import asyncio
import queue
from collections.abc import AsyncIterator
from typing import Any

from vihs_pod.pipeline.protocols import Partial


class FasterWhisperSTT:
    """Streaming STT via faster-whisper's `transcribe`.

    The input iterator yields 16 kHz mono PCM bytes (the pod's user_input
    data channel format). All PCM is buffered until the endpoint, then
    transcribed once — matching the turn FSM's ENDPOINT → OPEN_STT flow
    (SPEC-001 D2). faster-whisper is synchronous, so transcription runs on
    a worker thread bridged back through a queue.
    """

    def __init__(
        self,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        model_dir: str | None = None,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.model_dir = model_dir
        self._model = None

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]  # noqa: F401

            # model_dir points at the network-volume stt/ layout when set
            # (VOLUME.md); otherwise faster-whisper downloads from HF hub.
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                **({"download_root": self.model_dir} if self.model_dir else {}),
            )
        return self._model

    async def stream(self, pcm: AsyncIterator[bytes]) -> AsyncIterator[Partial]:
        model = self._load()
        buffer = bytearray()

        async def collect() -> None:
            async for data in pcm:
                buffer.extend(data)

        collector = asyncio.create_task(collect())
        q: queue.Queue[Partial | None] = queue.Queue()
        worker: asyncio.Task[None] | None = None
        try:
            await collector
            # Transcribe the accumulated utterance on a thread (sync lib).
            worker = asyncio.create_task(asyncio.to_thread(_transcribe, model, buffer, q))
            while True:
                item = await asyncio.to_thread(q.get)
                if item is None:
                    break
                yield item
            if worker is not None:
                await worker
        finally:
            collector.cancel()
            if worker is not None:
                worker.cancel()


def _transcribe(model: Any, buffer: bytearray, q: queue.Queue[Partial | None]) -> None:
    """Sync faster-whisper call on a worker thread (bridged via `q`)."""
    segments, _info = model.transcribe(bytes(buffer), beam_size=1, condition_on_previous_text=False)
    for seg in segments:
        q.put(Partial(text=seg.text.strip(), final=True))
    q.put(None)

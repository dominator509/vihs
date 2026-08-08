"""Real TTS stage adapter (EP-005 M7): Piper process.

Config-gated: the Piper binary + voice model are external; CI never
invokes them. The adapter spawns `piper` as a subprocess, feeds clause
text on stdin, and reads raw 16 kHz mono PCM on stdout, chunking it into
`AudioChunk`s with `chars_covered` proportional to the clause length —
satisfying the ledger's chars/ms contract (SPEC-001 D3, INV-1).

Cancel-close: on generator close the process is terminated (no orphaned
synth work).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from vihs_pod.pipeline.protocols import AudioChunk


class PiperTTS:
    """Piper via `echo <clause> | piper --model voice.onnx` (16 kHz mono)."""

    def __init__(
        self,
        binary: str = "piper",
        voice: str = "en_US-lessac-medium.onnx",
        sample_rate: int = 16000,
        chunk_ms: int = 100,
    ) -> None:
        self.binary = binary
        self.voice = voice
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self._proc: asyncio.subprocess.Process | None = None

    async def _ensure_proc(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            self._proc = await asyncio.create_subprocess_exec(
                self.binary,
                "--model",
                self.voice,
                "--output-raw",
                "--output-dir",
                "/dev/stdout",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        return self._proc

    async def stream(self, clause: str, voice: str) -> AsyncIterator[AudioChunk]:
        proc = await self._ensure_proc()
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write((clause + "\n").encode())
            await proc.stdin.drain()

            n = len(clause)
            if n == 0:
                return
            bytes_per_chunk = int(self.sample_rate * 2 * (self.chunk_ms / 1000.0))
            dur_ms_total = int(n * 10)  # ~10 ms per char, matches mock budget
            chunks = max(1, dur_ms_total // self.chunk_ms)
            chars_per_chunk = n / chunks

            remaining = dur_ms_total
            covered = 0.0
            while remaining > 0 and covered < n:
                data = await proc.stdout.read(bytes_per_chunk)
                if not data:
                    break
                this_ms = min(self.chunk_ms, remaining)
                start_c = int(covered)
                end_c = min(n, int(covered + chars_per_chunk))
                yield AudioChunk(pcm=data, dur_ms=this_ms, chars_covered=max(0, end_c - start_c))
                covered += chars_per_chunk
                remaining -= this_ms
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except TimeoutError:
                    proc.kill()

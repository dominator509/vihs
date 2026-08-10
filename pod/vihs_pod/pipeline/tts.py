"""Real TTS stage adapter (EP-005 M7): Piper.

Config-gated: the Piper voice model is external; CI never invokes it.
The adapter runs Piper IN-PROCESS via `piper.voice.PiperVoice` (EP-010
M2): the model is loaded once and kept resident, so per-clause synthesis
is ~88ms for a short clause instead of the ~3.5s ONNX init a subprocess
spawn pays on every session. `synthesize()` returns exactly the audio for
one clause — no pipe leftovers, no estimated-duration drift.

The subprocess path (`PiperTTS._ensure_proc` / CLI) is retained for the
fake-proc unit test and as a fallback when the in-process voice is not
loaded.

Cancel-close: on generator close the process is terminated (no orphaned
synth work). The pod-level shared instance (get_shared_piper) is closed
only at pod shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from vihs_pod.pipeline.protocols import AudioChunk

_shared: "PiperTTS | None" = None


def get_shared_piper(
    binary: str = "piper",
    voice: str = "en_US-lessac-medium.onnx",
    sample_rate: int = 16000,
    chunk_ms: int = 100,
    cuda: bool = False,
) -> "PiperTTS":
    """Pod-level singleton: ONE PiperTTS for the pod lifetime.

    EP-010 M2: piper pays ~3.5s of ONNX init per process spawn. A
    per-session process paid that on every revoke (the 4252ms tts_ttfa
    wall). Sessions share this instance; the model stays resident and a
    warm clause is ~88ms. The instance is closed only at pod shutdown
    (see `close_pod_piper`).
    """
    global _shared
    if _shared is None:
        _shared = PiperTTS(
            binary=binary,
            voice=voice,
            sample_rate=sample_rate,
            chunk_ms=chunk_ms,
            cuda=cuda,
        )
        _shared.pod_shared = True
    return _shared


async def close_pod_piper() -> None:
    """Pod-shutdown teardown for the shared piper (revoke path must NOT
    close it — that is what caused the per-session reload wall)."""
    global _shared
    if _shared is not None:
        await _shared.close()
        _shared = None


class PiperTTS:
    """Piper TTS — in-process voice (default) or CLI subprocess (fallback)."""

    def __init__(
        self,
        binary: str = "piper",
        voice: str = "en_US-lessac-medium.onnx",
        sample_rate: int = 16000,
        chunk_ms: int = 100,
        cuda: bool = False,
    ) -> None:
        self.binary = binary
        self.voice = voice
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.cuda = cuda
        self.pod_shared = False  # set True by get_shared_piper
        self._voice: object | None = None  # piper.voice.PiperVoice (lazy)
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure_proc(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            args = [
                self.binary,
                "--model",
                self.voice,
                "--output-raw",
                "--output-dir",
                "/dev/stdout",
            ]
            if self.cuda:
                args.append("--cuda")
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        return self._proc

    async def warmup(self) -> None:
        """Load the Piper voice IN-PROCESS so the ONNX model is resident
        before the first real session (EP-010 M2: in-process synthesis of
        a short clause is ~88ms; the subprocess fallback pays ~3.5s of
        ONNX init on every spawn). Best-effort: failure falls back to the
        CLI path at stream time."""
        if self._voice is not None:
            return
        try:
            from piper.voice import PiperVoice  # noqa: PLC0415

            self._voice = await asyncio.to_thread(
                PiperVoice.load, self.voice, None, self.cuda
            )
        except Exception:  # noqa: BLE001 — fall back to CLI path
            self._voice = None

    async def _synthesize_in_process(
        self, clause: str
    ) -> AsyncIterator[AudioChunk]:
        """Synthesize one clause in-process; chunk into AudioChunks by
        REAL audio duration (no estimated-duration drift, no pipe
        leftovers)."""
        voice = self._voice
        assert voice is not None
        chunks = await asyncio.to_thread(
            lambda: list(voice.synthesize(clause))
        )
        n = len(clause)
        if n == 0:
            return
        pcm = b"".join(c.audio_int16_bytes for c in chunks)
        total_ms = int(len(pcm) / (self.sample_rate * 2) * 1000)
        bytes_per_chunk = int(self.sample_rate * 2 * (self.chunk_ms / 1000.0))
        if total_ms <= 0:
            return
        chunks_out = max(1, total_ms // self.chunk_ms)
        chars_per_chunk = n / chunks_out
        covered = 0.0
        remaining = total_ms
        offset = 0
        while remaining > 0 and covered < n:
            data = pcm[offset : offset + bytes_per_chunk]
            if not data:
                break
            offset += len(data)
            this_ms = min(self.chunk_ms, remaining)
            start_c = int(covered)
            end_c = min(n, int(covered + chars_per_chunk))
            yield AudioChunk(
                pcm=data,
                dur_ms=this_ms,
                chars_covered=max(0, end_c - start_c),
            )
            covered += chars_per_chunk
            remaining -= this_ms

    async def stream(self, clause: str, voice: str) -> AsyncIterator[AudioChunk]:
        # EP-010 M2: in-process synthesis is the fast path (model resident,
        # ~88ms short clause). The lock serializes concurrent sessions on
        # the SHARED instance (one ONNX session; onnxruntime is not
        # guaranteed thread-safe for concurrent synthesize calls).
        async with self._lock:
            if self._voice is not None:
                async for ch in self._synthesize_in_process(clause):
                    yield ch
                return
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
                # Keep the process alive across clauses (EP-009): piper reads
                # lines from stdin until EOF, so ONE process serves the whole
                # turn. Terminating per clause forced a full model reload per
                # clause (~3.3s local, more from the network volume) and blew
                # the turn budget. close() below is the real teardown path.
                pass

    async def close(self) -> None:
        """Terminate the persistent piper process (revoke path)."""
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except TimeoutError:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=1.0)

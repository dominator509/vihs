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
import os
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from vihs_pod.pipeline.clause import ABBREV
from vihs_pod.pipeline.protocols import AudioChunk

if TYPE_CHECKING:
    from piper.voice import PiperVoice as _PiperVoice

_shared: PiperTTS | None = None


def _split_sentences(clause: str) -> list[str]:
    """Abbreviation-aware sentence split (mirrors the clause chunker's
    boundary rules: no split on Mr./Dr./etc., decimals, or ellipsis dots).

    Used BOTH to drive per-sentence synthesis/prosody AND to allocate
    chars_covered. Piper's espeak phonemizer keeps abbreviations as one
    sentence; a naive split would synthesize "Mr." alone and sound wrong.
    """
    if not clause:
        return []
    out: list[str] = []
    start = 0
    i = 0
    n = len(clause)
    while i < n:
        ch = clause[i]
        nxt = clause[i + 1 : i + 2]
        if ch in ".?!" and (nxt == "" or nxt == " "):
            tail = clause[max(0, i - 4) : i + 1].lower()
            if any(tail.endswith(a) for a in ABBREV):
                i += 1
                continue
            if ch == "." and clause[i - 1 : i].isdigit() and nxt.isdigit():
                i += 1
                continue
            if ch == "." and (clause[i - 1 : i] == "." or nxt == "."):
                i += 1
                continue
            out.append(clause[start : i + 1])
            start = i + 1
            # The space after the terminal is a gap, not speech — strip
            # it so sentence text (and its char budget) is exact.
            while start < n and clause[start] in " \t\n":
                start += 1
        i += 1
    if start < n:
        out.append(clause[start:])
    return [s for s in out if s.strip()]


def _pause_ms_after(sentence: str) -> int:
    """Natural inter-sentence pause (ms) driven by final punctuation.

    Human speech breathes between thoughts: a period gets a beat, a
    question lingers a little longer, an ellipsis means trailing off,
    a comma is a short lift. This is the cadence layer (EP-010 M2):
    without it piper chains every sentence at machine tempo.
    """
    s = sentence.rstrip()
    if s.endswith("...") or s.endswith("…"):
        return 600
    if s.endswith("?"):
        return 380
    if s.endswith("!"):
        return 340
    if s.endswith("."):
        return 320
    if s.endswith(","):
        return 180
    return 250


def _prosody_for(
    sentence: str,
    base_length: float,
    base_noise: float,
    base_noise_w: float,
):
    """Per-sentence SynthesisConfig from emotion cues in the text.

    Exclamation → brighter, a touch faster (energy). Question → slightly
    slower (curiosity). Ellipsis → slower and quieter (trailing off).
    ALL-CAPS words → deliberate emphasis (slower + louder). These are
    small, deterministic deltas — the goal is natural variation, not
    cartoon acting.
    """
    from piper.config import SynthesisConfig  # noqa: PLC0415

    s = sentence.strip()
    length = base_length
    noise = base_noise
    volume = 1.0

    excl = s.count("!")
    if excl:
        length *= max(0.90, 1.0 - 0.035 * excl)
        noise = min(0.85, noise + 0.04 * excl)
    if s.rstrip().endswith("?"):
        length *= 1.03
    if "..." in s or s.endswith("…"):
        length *= 1.08
        noise = max(0.55, noise - 0.05)
        volume *= 0.94
    words = re.findall(r"[A-Z]{2,}", s)
    if words:
        length *= 1.02
        volume = min(1.25, volume + 0.07 * len(words))

    return SynthesisConfig(
        length_scale=round(length, 3),
        noise_scale=round(noise, 3),
        noise_w_scale=base_noise_w,
        volume=round(volume, 3),
    )


def get_shared_piper(
    binary: str = "piper",
    voice: str = "en_US-lessac-medium.onnx",
    sample_rate: int = 16000,
    chunk_ms: int = 100,
    cuda: bool = False,
) -> PiperTTS:
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
        self._voice: _PiperVoice | None = None  # piper.voice.PiperVoice (lazy)
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
        CLI path at stream time.

        The ONNX session is created with a BOUNDED intra-op thread pool
        (VIHS_TTS_THREADS, default 2): onnxruntime's default of 0 means
        "all cores", which thrashes against llama-server on the same
        vCPUs — measured: tts_ttfa 1.4–1.7s on the pod vs ~300ms local
        with the same voice. A short clause parallelizes poorly past 2
        threads, so bounding costs ~nothing while leaving llama-server
        headroom.
        """
        import logging

        log = logging.getLogger("vihs_pod.pipeline.tts")
        if self._voice is not None:
            return
        try:
            import json

            import onnxruntime  # type: ignore[import-untyped]
            from piper.config import PiperConfig
            from piper.phonemize_espeak import ESPEAK_DATA_DIR
            from piper.voice import PiperVoice  # noqa: PLC0415

            threads = int(os.environ.get("VIHS_TTS_THREADS", "2"))
            providers: list[tuple[str, dict[str, str]]] = (
                [("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"})]
                if self.cuda
                else [("CPUExecutionProvider", {})]
            )
            opts = onnxruntime.SessionOptions()
            if threads > 0:
                opts.intra_op_num_threads = threads
            with open(f"{self.voice}.json", encoding="utf-8") as f:
                config = PiperConfig.from_dict(json.load(f))
            session = onnxruntime.InferenceSession(
                self.voice, sess_options=opts, providers=providers
            )
            self._voice = PiperVoice(
                session=session,
                config=config,
                espeak_data_dir=ESPEAK_DATA_DIR,
                download_dir=None,  # type: ignore[arg-type]  # noqa: PLC0415
            )
            log.info(
                "tts warmup OK voice=%s cuda=%s threads=%d",
                self.voice,
                self.cuda,
                threads,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to CLI path
            self._voice = None
            log.warning("tts warmup FAILED voice=%s: %s", self.voice, exc)

    async def _synthesize_in_process(self, clause: str) -> AsyncIterator[AudioChunk]:
        """Synthesize one clause in-process; chunk into AudioChunks by
        REAL audio duration (no estimated-duration drift, no pipe
        leftovers).

        EP-010 M2: each SENTENCE is synthesized with its own
        `SynthesisConfig` (speed/energy/volume from punctuation and
        emotion cues — the cadence layer: questions linger, exclamations
        brighten, ellipses trail off) and a natural pause is inserted
        after it. We yield audio AS IT IS PRODUCED (not after the whole
        clause): the FIRST sentence's audio is ready in ~100ms even for a
        long multi-sentence clause — that is what keeps tts_ttfa near the
        first-sentence cost instead of the whole-clause cost. (A prior
        version did `list(voice.synthesize(...))`, which materialized the
        entire clause — a 3-sentence clause paid ~1.5s before the first
        chunk.)

        Audio rate: the voice model's OWN sample rate (22050 for
        en_US-lessac-medium) governs byte math; a fixed 16000 assumption
        made every chunk ~27% too short in dur_ms AND broke the
        char-allocation heuristic (expected_total_bytes was ~10x smaller
        than real audio, so `covered < n` terminated the loop after ~2
        chunks and discarded the rest of the clause).

        Threading: the ENTIRE synthesis loop runs inside ONE executor
        thread (onnxruntime/espeak state is not safe to poke with next()
        from multiple threads — that deadlocks). Each produced sentence
        is pushed to an asyncio.Queue via call_soon_threadsafe (a plain
        cross-thread put_nowait only appends to _ready and does NOT
        write the self-pipe — if the loop is blocked in epoll the item
        is never observed: that was the streaming hang). This coroutine
        consumes it sentence by sentence, so first audio still streams
        at ~100ms while the worker keeps synthesizing the rest.

        chars_covered: each sentence's chars are distributed across its
        audio chunks by byte fraction; the LAST sentence absorbs the
        remainder so the meter lands exactly on len(clause). Pause
        chunks carry chars_covered=0 (silence is not text progress).
        """
        voice = self._voice
        assert voice is not None
        n = len(clause)
        if n == 0:
            return
        sentences = _split_sentences(clause)
        if not sentences:
            return
        sr = int(getattr(getattr(voice, "config", None), "sample_rate", self.sample_rate))
        bytes_per_chunk = max(1, int(sr * 2 * (self.chunk_ms / 1000.0)))
        base_length = float(getattr(getattr(voice, "config", None), "length_scale", 1.0))
        base_noise = float(getattr(getattr(voice, "config", None), "noise_scale", 0.667))
        base_noise_w = float(getattr(getattr(voice, "config", None), "noise_w_scale", 0.8))
        # Unbounded queue + call_soon_threadsafe (see docstring).
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _run() -> None:
            try:
                for idx, sentence in enumerate(sentences):
                    cfg = _prosody_for(sentence, base_length, base_noise, base_noise_w)
                    pcms = [c.audio_int16_bytes for c in voice.synthesize(sentence, syn_config=cfg)]
                    pcms = [p for p in pcms if p]
                    if not pcms:
                        continue
                    loop.call_soon_threadsafe(queue.put_nowait, ("audio", idx, pcms))
                    pause_ms = _pause_ms_after(sentence)
                    if pause_ms > 0:
                        loop.call_soon_threadsafe(queue.put_nowait, ("pause", pause_ms))
            except Exception:  # noqa: BLE001 — one bad sentence must not kill the turn
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = asyncio.create_task(asyncio.to_thread(_run))
        covered = 0.0
        while True:
            item = await queue.get()
            if item is None:
                break
            kind = item[0]
            if kind == "pause":
                pause_ms = item[1]
                silence_bytes = int(sr * 2 * pause_ms / 1000)
                off = 0
                while off < silence_bytes:
                    data = b"\x00" * min(bytes_per_chunk, silence_bytes - off)
                    if not data:
                        break
                    this_ms = int(len(data) / (sr * 2) * 1000)
                    yield AudioChunk(pcm=data, dur_ms=this_ms, chars_covered=0)
                    off += len(data)
                continue

            # audio: ("audio", sentence_index, [pcm, ...])
            idx = item[1]
            pcms = item[2]
            total_len = sum(len(p) for p in pcms)
            if total_len <= 0:
                continue
            total_ms = int(total_len / (sr * 2) * 1000)
            if total_ms <= 0:
                continue
            # Char budget for THIS sentence. The LAST sentence absorbs
            # whatever chars remain (separators, split drift) so the
            # meter ends exactly at len(clause).
            if idx < len(sentences) - 1:
                this_sent_chars = float(len(sentences[idx]))
            else:
                this_sent_chars = max(0.0, n - covered)
            this_sent_chars = min(this_sent_chars, n - covered)
            is_last_sentence = idx == len(sentences) - 1
            bank = 0.0  # fractional-char accumulator (floor per chunk leaks)
            for pcm in pcms:
                sent_len = len(pcm)
                if sent_len <= 0:
                    continue
                offset = 0
                remaining_ms = int(sent_len / (sr * 2) * 1000)
                is_last_pcm = pcm is pcms[-1]
                while remaining_ms > 0:
                    data = pcm[offset : offset + bytes_per_chunk]
                    if not data:
                        break
                    offset += len(data)
                    this_ms = min(self.chunk_ms, remaining_ms)
                    frac = len(data) / total_len
                    alloc = this_sent_chars * frac + bank
                    chunk_chars = int(alloc)
                    bank = alloc - chunk_chars
                    # Tail: last chunk of the last sentence lands on n.
                    if is_last_sentence and is_last_pcm and remaining_ms - this_ms <= 0:
                        chunk_chars = int(n) - int(covered)
                    chunk_chars = max(0, chunk_chars)
                    yield AudioChunk(
                        pcm=data,
                        dur_ms=this_ms,
                        chars_covered=chunk_chars,
                    )
                    covered += chunk_chars
                    remaining_ms -= this_ms
        with contextlib.suppress(Exception):
            await task

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
                    yield AudioChunk(
                        pcm=data,
                        dur_ms=this_ms,
                        chars_covered=max(0, end_c - start_c),
                    )
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

"""Client-driven conversation over the orchestrator relay (EP-005 M5).

The pod's RTCPeerConnection is driven by REAL client signaling frames:
offer/ice travel client → `/v1/signal/{connection_id}` (orchestrator) →
pod `/internal/pods/{id}/signal` WS → `SignalBridge` → this module, and
answer/ice go back the same way. User input arrives on the `user_input`
data channel (mock STT in CI; real STT in M7). Responses run the
clause-pipelined task graph with LIVE captions on the `captions` channel;
barge-in aborts via AbortBus and commits the INV-1 partial exactly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription

from vihs_pod.append_buffer import AppendBuffer
from vihs_pod.captions import CaptionsChannel
from vihs_pod.health import SignalBridge
from vihs_pod.memory_client import MemoryClient
from vihs_pod.metrics import Metrics, PodMetrics
from vihs_pod.pipeline.abort_bus import AbortBus, PlayedSpan
from vihs_pod.pipeline.flow import StageCrashError, abort_response, run_response
from vihs_pod.pipeline.ledger import PartialChunk, committed_text
from vihs_pod.pipeline.protocols import AudioChunk
from vihs_pod.webrtc_loopback import wait_gathering_complete

# SPEC-006 row 1: the fixed recovery utterance spoken after a pipeline
# stage crash (SPEC-001 "apologize-utterance path, fixed string").
RECOVERY_UTTERANCE = "Something went wrong on my end. Give me a moment."

log = logging.getLogger("vihs_pod.conversation")


class PlaybackMonitor:
    """Tracks the audio chunk currently on-wire so barge-in can compute the
    INV-1 partial cut from REAL elapsed playback time."""

    def __init__(self, mux: Any) -> None:
        self._mux = mux
        self._current: PartialChunk | None = None
        self._started = 0.0

    async def push(self, item: Any, clause_id: int, span: tuple[int, int]) -> None:
        if isinstance(item, AudioChunk):
            self._current = PartialChunk(
                clause_id=clause_id,
                char_start=span[0],
                chars_covered=item.chars_covered,
                dur_ms=item.dur_ms,
                played_ms=0,
            )
            self._started = time.monotonic()
        try:
            await self._mux.push(item, clause_id, span)
        finally:
            if isinstance(item, AudioChunk):
                self._current = None

    async def flush_and_report(self) -> list[PlayedSpan]:
        return cast(list[PlayedSpan], await self._mux.flush_and_report())

    @property
    def reported(self) -> bool:
        return bool(getattr(self._mux, "reported", False))

    def reset(self) -> None:
        """Per-turn reset: the playback ledger covers ONE response (INV-1).
        Must run at turn start — otherwise a barge-in during prompt assembly
        would commit the PREVIOUS turn's spans."""
        self._current = None
        self._started = 0.0
        if hasattr(self._mux, "reset"):
            self._mux.reset()

    def partial(self) -> PartialChunk | None:
        """The in-flight chunk (if any) with played_ms measured from real
        elapsed time since playback started."""
        if self._current is None:
            return None
        played_ms = int((time.monotonic() - self._started) * 1000)
        return replace(self._current, played_ms=min(played_ms, self._current.dur_ms))


def build_stages(
    answers: list[str] | None = None, *, real: bool = False
) -> tuple[SimpleNamespace, PlaybackMonitor]:
    """Stage container for one conversation.

    Mock stages are CI-safe and deterministic (the E2E gate). `real=True`
    constructs the M7 real adapters, config-gated by env: the LLM stage is
    the ADR-012 axiom-gateway provider when `PROVIDER=axiom-gateway`,
    local vLLM when `PROVIDER=vllm`, and the mock scripted LLM otherwise.
    Real TTS/STT/VAD/LipSync/Mux constructors are lazy (they only touch
    subprocess/GStreamer at first stream()), so building them here is safe
    even where the binaries are absent.
    """
    if real:
        import os

        from vihs_pod.pipeline.lipsync import StubLipSync
        from vihs_pod.pipeline.llm import VLLMLLM, AxiomGatewayLLM
        from vihs_pod.pipeline.mux import GStreamerMux
        from vihs_pod.pipeline.tts import get_shared_piper

        provider = os.environ.get("PROVIDER", "mock")
        llm_url = os.environ.get("VIHS_LLM_URL", "http://127.0.0.1:8000/v1")
        llm_token = os.environ.get("VIHS_LLM_TOKEN", "")
        if provider == "axiom-gateway":
            llm: Any = AxiomGatewayLLM(
                url=llm_url,
                token=llm_token,
                model=os.environ.get("VIHS_LLM_MODEL") or None,
                provider=os.environ.get("VIHS_LLM_PROVIDER", "") or None,
                egress=os.environ.get("VIHS_LLM_EGRESS", "1") != "0",
                verify=os.environ.get("VIHS_LLM_TLS_VERIFY", "1") != "0",
            )
        elif provider == "vllm":
            llm = VLLMLLM(url=llm_url, token=llm_token or None)
        else:
            from vihs_pod.mocks.stages import ScriptedLLM

            llm = ScriptedLLM(answers or [])

        # Real STT/TTS read their model paths from VIHS_MODEL_DIR (the
        # network-volume mount, VOLUME.md layout): stt/ and tts/. Fall back
        # to the adapter defaults when the env var is unset (local dev).
        model_dir = os.environ.get("VIHS_MODEL_DIR", "")
        if model_dir:
            from vihs_pod.pipeline.stt import FasterWhisperSTT

            stt: Any = FasterWhisperSTT(
                model_size=os.environ.get("VIHS_STT_MODEL", "base"),
                device=os.environ.get("VIHS_STT_DEVICE", "cuda"),
                compute_type=os.environ.get("VIHS_STT_COMPUTE", "float16"),
                model_dir=os.path.join(model_dir, "stt"),
            )
            tts: Any = get_shared_piper(
                binary=os.environ.get("VIHS_TTS_BIN", "piper"),
                voice=os.environ.get(
                    "VIHS_TTS_VOICE", os.path.join(model_dir, "tts", "en_US-lessac-medium.onnx")
                ),
                cuda=os.environ.get("VIHS_TTS_CUDA", "0") == "1",
            )
        else:
            stt = None
            tts = get_shared_piper()
        lipsync: Any = StubLipSync()
        mux: Any = GStreamerMux()
        monitor = PlaybackMonitor(mux)
        # VAD is built but not wired into the mock user-input path (the E2E
        # harness types text); it is exercised by its own unit tests.
        return SimpleNamespace(llm=llm, stt=stt, tts=tts, lipsync=lipsync, mux=monitor), monitor

    # EP-007 M4: the capacity harness injects mock stage latencies via env so
    # CI can simulate realistic per-stage budgets without a GPU host. Defaults
    # stay 0 (existing tests unchanged). Values are milliseconds.
    import os

    from vihs_pod.mocks.stages import MockLipSync, MockMux, MockTTS, ScriptedLLM

    llm_ttft_ms = float(os.environ.get("VIHS_MOCK_LLM_TTFT_MS", "0")) / 1000.0
    tts_ttfa_ms = float(os.environ.get("VIHS_MOCK_TTS_TTFA_MS", "0")) / 1000.0
    lipsync_ff_ms = float(os.environ.get("VIHS_MOCK_LIPSYNC_FF_MS", "0")) / 1000.0

    llm = ScriptedLLM(answers or [], ttft=llm_ttft_ms)
    # EP-007 M5: env-gated fault hook (SPEC-006 row 1). `VIHS_FAULT=stage_crash`
    # wraps the LLM so the FIRST streamed token raises — the chaos drill
    # (tests/chaos/stage_crash.py) proves the recovery path deterministically.
    if os.environ.get("VIHS_FAULT", "") == "stage_crash":
        from vihs_pod.mocks.stages import StageCrashLLM

        llm = StageCrashLLM(llm)
    tts = MockTTS(ms_per_char=10, ttfa=tts_ttfa_ms)
    lipsync = MockLipSync(ff=lipsync_ff_ms)
    monitor = PlaybackMonitor(MockMux())
    return SimpleNamespace(llm=llm, tts=tts, lipsync=lipsync, mux=monitor), monitor


class Conversation:
    """One assigned session's live conversation (client ↔ pod)."""

    def __init__(
        self,
        session_id: str,
        connection_id: str,
        pod_token: str,
        cursor: dict[str, Any],
        memory: MemoryClient,
        stages: SimpleNamespace,
        monitor: PlaybackMonitor,
        pod_metrics: PodMetrics | None = None,
    ) -> None:
        self.session_id = session_id
        self.connection_id = connection_id
        self.pod_token = pod_token
        self.cursor = cursor
        self.memory = memory
        self.stages = stages
        self.monitor = monitor
        self.bus = AbortBus()
        self.ledger: dict[int, str] = {}
        self.bridge = SignalBridge()
        self.pc = RTCPeerConnection()
        self._captions: CaptionsChannel | None = None
        self._user_input: asyncio.Queue[str] = asyncio.Queue()
        self._turn_id = int(cursor.get("last_turn_id", 0))
        self._responding: asyncio.Task[Any] | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        # SPEC-006 row 1: stage crashes in THIS session. Two → the pod
        # marks itself degraded and the orchestrator drains it.
        self.stage_crashes = 0
        self.degraded = False
        # R2 append buffer (ARCHITECTURE §9): committed events are queued and
        # flushed in the background — the media path never blocks on memoryd.
        self.append_buffer = AppendBuffer(memory, session_id)
        # Per-stage first-chunk histograms (SPEC-007 O1, ARCHITECTURE §6):
        # the capacity harness (EP-007 M4) reads these via pod /health.
        self.metrics = Metrics()
        # Pod-level counters/gauges (EP-008 M2): barge-in, abort flush,
        # endpoint-premature. Shared across conversations on the pod.
        self.pod_metrics = pod_metrics or PodMetrics()

    async def start(self) -> None:
        @self.pc.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if channel.label == "captions":
                self._captions = CaptionsChannel(channel)
                log.info("captions channel open")
            elif channel.label == "user_input":
                channel.on("message", lambda m: self._user_input.put_nowait(str(m)))
                log.info("user_input channel open")

        self._tasks.append(asyncio.create_task(self._signaling_loop()))
        self._tasks.append(asyncio.create_task(self._response_loop()))
        self.append_buffer.start()

    # --- signaling ---

    async def _signaling_loop(self) -> None:
        while True:
            frame = await self.bridge.inbound.get()
            t = frame.get("t")
            if t == "offer":
                await self.pc.setRemoteDescription(
                    RTCSessionDescription(sdp=frame["sdp"], type="offer")
                )
                answer = await self.pc.createAnswer()
                await self.pc.setLocalDescription(answer)
                await wait_gathering_complete(self.pc)
                await self.bridge.outbound.put({"t": "answer", "sdp": self.pc.localDescription.sdp})
            elif t == "ice":
                await self.pc.addIceCandidate(RTCIceCandidate(**frame["candidate"]))

    # --- conversation ---

    async def _response_loop(self) -> None:
        while True:
            user_text = await self._user_input.get()
            if self._responding is not None and not self._responding.done():
                await self._barge_in()
            # Spawn, don't await: the loop must stay live so the NEXT input
            # can barge in mid-response (SPEC-001 D3).
            self._responding = asyncio.create_task(self._handle_turn(user_text))

    async def _handle_turn(self, user_text: str) -> None:
        self._turn_id += 1
        # Per-turn playback ledger reset FIRST: the mux must not carry spans
        # from the previous turn into this one (INV-1).
        self.monitor.reset()
        self.ledger.clear()
        await self._append_event("user", user_text, interrupted=False)
        prompt = await self._build_prompt(user_text)
        gen = self.bus.fresh()
        try:
            committed = await run_response(
                gen,
                self.bus,
                self.stages,
                prompt,
                self._turn_id,
                self.ledger,
                on_caption=self._send_caption,
                metrics=self.metrics,
            )
        except StageCrashError as crash:
            # SPEC-006 row 1: a pipeline stage crashed mid-turn. run_response
            # already aborted the sibling stages cleanly (AbortBus) and
            # flushed the mux — resolve the INV-1 partial, emit the
            # `stage_error` note (no user text), speak the fixed recovery
            # utterance, and return to listening. Two crashes in one session
            # mark the pod degraded so the orchestrator drains it.
            await self._recover_stage_crash(crash)
            return
        await self._send_caption_final(self._turn_id)
        await self._append_event("assistant", committed.text, interrupted=False)
        log.info(
            "committed turn=%d interrupted=False chars=%d text=%r",
            self._turn_id,
            len(committed.text),
            committed.text[:60],
        )

    async def _recover_stage_crash(self, crash: StageCrashError) -> None:
        """SPEC-006 row 1 recovery: note + fixed utterance + re-listen."""
        self.stage_crashes += 1
        if self.stage_crashes >= 2:
            self.degraded = True
            log.warning(
                "pod degraded: %d stage crashes in session %s",
                self.stage_crashes,
                self.session_id,
            )
        # INV-1: commit exactly what played before the crash (run_response's
        # abort flushed the mux and attached the PlayedSpans), as an
        # interrupted turn — the transcript never claims unheard speech.
        partial = self.monitor.partial()
        partial_text = committed_text(self.ledger, crash.played, partial)
        if partial_text:
            await self._append_event("assistant", partial_text, interrupted=True)
        # The note records the failure; NO user text (OBSERVABILITY).
        await self._append_note(
            "stage_error",
            {"stage": crash.stage, "crashes": self.stage_crashes},
        )
        # Speak the fixed recovery utterance (SPEC-006 row 1) and re-listen.
        await self._append_event("assistant", RECOVERY_UTTERANCE, interrupted=False)
        await self._send_caption_final(self._turn_id)
        log.warning(
            "stage crash recovered turn=%d stage=%s crashes=%d",
            self._turn_id,
            crash.stage,
            self.stage_crashes,
        )

    async def _barge_in(self) -> None:
        """Abort the in-flight response, commit the INV-1 partial, and log."""
        partial = self.monitor.partial()
        started = time.perf_counter()
        committed = await abort_response(self.bus, self.stages, self.ledger, partial)
        flush_ms = (time.perf_counter() - started) * 1000.0
        # EP-008 M2: turn-taking metrics (registry OBSERVABILITY.md).
        self.pod_metrics.record_bargein(flush_ms)
        if self._responding is not None:
            self._responding.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._responding
            self._responding = None
        await self._append_event("assistant", committed.text, interrupted=True)
        log.info(
            "committed turn=%d interrupted=True chars=%d text=%r",
            self._turn_id,
            len(committed.text),
            committed.text[:60],
        )

    async def _send_caption(self, turn_id: int, delta: str) -> None:
        if self._captions is not None:
            await self._captions.send(turn_id, delta, final=False)

    async def _send_caption_final(self, turn_id: int) -> None:
        if self._captions is not None:
            await self._captions.send(turn_id, "", final=True)

    async def _append_event(self, role: str, text: str, interrupted: bool) -> None:
        event = {
            "v": 1,
            "session_id": self.session_id,
            "turn_id": self._turn_id,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "role": role,
            "kind": "utterance",
            "text": text,
            "meta": {"interrupted": interrupted},
        }
        # R2: enqueue and return — the flusher drains to memoryd with
        # retry/backoff. The media path never awaits memoryd here.
        self.append_buffer.enqueue(event)

    async def _append_note(self, kind: str, meta: dict[str, object]) -> None:
        """System note event (SPEC-006 row 1): `kind:note`, NO user text.

        The `stage_error` note records the failure for the durable log
        without echoing transcript content (OBSERVABILITY redaction). The
        recovery utterance is appended separately as an assistant event.
        """
        event = {
            "v": 1,
            "session_id": self.session_id,
            "turn_id": self._turn_id,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "role": "system",
            "kind": "note",
            "text": "",
            "meta": meta,
        }
        self.append_buffer.enqueue(event)

    async def _build_prompt(self, user_text: str) -> str:
        """S3-live-turns source: the durable transcript (mock LLM ignores
        content; real adapters use PromptSegments from context.py in M7)."""
        try:
            prior = await self.memory.transcript(self.session_id)
        except Exception:  # noqa: BLE001 — prompt assembly is best-effort
            prior = ""
        return f"{prior}\nuser: {user_text}"

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._responding is not None:
            self._responding.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # Best-effort flush of queued events before closing (revoke path).
        with contextlib.suppress(Exception):  # memoryd may be down at revoke
            await self.append_buffer.flush()
        await self.append_buffer.stop()
        # EP-009: close real stage subprocesses (persistent piper keeps the
        # model loaded across clauses — must be torn down at revoke).
        # EP-010 M2: the pod-level SHARED piper is NOT closed here — closing
        # it per session forced a ~3.5s ONNX reload on every revoke (the
        # 4252ms tts_ttfa wall). Only pod shutdown closes it
        # (close_pod_piper in agent.py).
        tts_close = getattr(self.stages.tts, "close", None)
        if tts_close is not None and not getattr(self.stages.tts, "pod_shared", False):
            with contextlib.suppress(Exception):
                await tts_close()
        await self.pc.close()

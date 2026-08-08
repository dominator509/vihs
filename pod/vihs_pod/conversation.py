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
from vihs_pod.pipeline.abort_bus import AbortBus, PlayedSpan
from vihs_pod.pipeline.flow import abort_response, run_response
from vihs_pod.pipeline.ledger import PartialChunk
from vihs_pod.pipeline.protocols import AudioChunk
from vihs_pod.webrtc_loopback import wait_gathering_complete

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
        from vihs_pod.pipeline.tts import PiperTTS

        provider = os.environ.get("PROVIDER", "mock")
        llm_url = os.environ.get("VIHS_LLM_URL", "http://127.0.0.1:8000/v1")
        llm_token = os.environ.get("VIHS_LLM_TOKEN", "")
        if provider == "axiom-gateway":
            llm: Any = AxiomGatewayLLM(url=llm_url, token=llm_token)
        elif provider == "vllm":
            llm = VLLMLLM(url=llm_url, token=llm_token or None)
        else:
            from vihs_pod.mocks.stages import ScriptedLLM

            llm = ScriptedLLM(answers or [])
        tts: Any = PiperTTS()
        lipsync: Any = StubLipSync()
        mux: Any = GStreamerMux()
        monitor = PlaybackMonitor(mux)
        # VAD is built but not wired into the mock user-input path (the E2E
        # harness types text); it is exercised by its own unit tests.
        return SimpleNamespace(llm=llm, tts=tts, lipsync=lipsync, mux=monitor), monitor

    from vihs_pod.mocks.stages import MockLipSync, MockMux, MockTTS, ScriptedLLM

    llm = ScriptedLLM(answers or [])
    tts = MockTTS(ms_per_char=10)
    lipsync = MockLipSync()
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
        # R2 append buffer (ARCHITECTURE §9): committed events are queued and
        # flushed in the background — the media path never blocks on memoryd.
        self.append_buffer = AppendBuffer(memory, session_id)

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
        committed = await run_response(
            gen,
            self.bus,
            self.stages,
            prompt,
            self._turn_id,
            self.ledger,
            on_caption=self._send_caption,
        )
        await self._send_caption_final(self._turn_id)
        await self._append_event("assistant", committed.text, interrupted=False)
        log.info(
            "committed turn=%d interrupted=False chars=%d text=%r",
            self._turn_id,
            len(committed.text),
            committed.text[:60],
        )

    async def _barge_in(self) -> None:
        """Abort the in-flight response, commit the INV-1 partial, and log."""
        partial = self.monitor.partial()
        committed = await abort_response(self.bus, self.stages, self.ledger, partial)
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
        await self.pc.close()

"""Adapter unit tests (EP-005 M7) — faked transports, no real deps.

Validates each real-stage adapter against its Protocol contract using
faked HTTP (httpx MockTransport) and faked subprocess (asyncio subprocess
stubs). No GPU, no GStreamer, no model weights — CI-safe by design.
"""

from __future__ import annotations

import json

import httpx

from vihs_pod.pipeline.lipsync import StubLipSync
from vihs_pod.pipeline.llm import VLLMLLM, AxiomGatewayLLM
from vihs_pod.pipeline.mux import GStreamerMux
from vihs_pod.pipeline.protocols import AudioChunk, Frame
from vihs_pod.pipeline.tts import PiperTTS
from vihs_pod.pipeline.vad import SileroVAD


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


async def test_axiom_gateway_parses_sse_chunks() -> None:
    """ADR-012: SSE `data: {"content": s}` chunks stream as text deltas."""
    events = [
        'data: {"content": "Hello"}',
        'data: {"content": " world"}',
        "data: [DONE]",
    ]
    payload = "\n\n".join(events) + "\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["egress"] is True
        assert body["policy"] == "latency"
        assert request.headers["authorization"] == "Bearer tok-123"
        return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})

    llm = AxiomGatewayLLM(
        url="http://gateway.test/api/v1/llm",
        token="tok-123",
        model="claude-opus-4-7",
    )
    llm._client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]

    # The adapter builds its own AsyncClient; test the transport seam by
    # patching httpx.AsyncClient to use the mock transport.
    original = httpx.AsyncClient

    class MockAsyncClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = MockAsyncClient  # type: ignore[misc]
    try:
        deltas = await _collect(llm.stream("user prompt"))
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert deltas == ["Hello", " world"]


async def test_vllm_parses_delta_content() -> None:
    events = [
        'data: {"choices": [{"delta": {"content": "A"}}]}',
        'data: {"choices": [{"delta": {"content": "B"}}]}',
        "data: [DONE]",
    ]
    payload = "\n\n".join(events) + "\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})

    original = httpx.AsyncClient

    class MockAsyncClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = MockAsyncClient  # type: ignore[misc]
    try:
        deltas = await _collect(VLLMLLM(url="http://127.0.0.1:8000/v1").stream("p"))
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert deltas == ["A", "B"]


async def test_axiom_gateway_error_surfaces() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    original = httpx.AsyncClient

    class MockAsyncClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = MockAsyncClient  # type: ignore[misc]
    try:
        try:
            await _collect(AxiomGatewayLLM(url="http://g/x", token="t").stream("p"))
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "401" in str(e)
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]


async def test_stub_lipsync_emits_timed_frames() -> None:
    audio = [AudioChunk(pcm=b"\x00" * 16, dur_ms=100, chars_covered=4)]
    frames = await _collect(StubLipSync().frames(_agen(audio)))
    assert isinstance(frames[0], Frame)
    assert frames[0].pts_ms == 0


async def _agen(items):
    for i in items:
        yield i


def test_silero_vad_energy_gate() -> None:
    vad = SileroVAD(threshold=0.05)
    assert not vad.is_speech(b"\x00\x00" * 400), "silence must be non-speech"
    loud = b"\xff\x7f" * 400  # high-amplitude 16-bit samples
    assert vad.is_speech(loud), "loud audio must be speech"


async def test_gstreamer_mux_ledger_shape() -> None:
    mux = GStreamerMux()
    await mux.push(AudioChunk(pcm=b"x" * 32, dur_ms=100, chars_covered=5), 1, (0, 5))
    await mux.push(Frame(data=b"y" * 8, pts_ms=0), 1, (0, 5))
    spans = await mux.flush_and_report()
    assert len(spans) == 1, "only audio chunks enter the INV-1 ledger"
    assert spans[0].clause_id == 1 and spans[0].char_start == 0 and spans[0].char_end == 5


async def test_piper_tts_fake_process() -> None:
    """Piper adapter contract with a faked subprocess emitting PCM."""
    tts = PiperTTS()

    class FakeStream:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._pos = 0

        def write(self, b: bytes) -> None:  # asyncio StreamWriter: sync
            pass

        async def drain(self) -> None:
            pass

        async def read(self, n: int) -> bytes:
            if self._pos >= len(self._data):
                return b""
            chunk = self._data[self._pos : self._pos + n]
            self._pos += len(chunk)
            return chunk

    class FakeProc:
        returncode = None
        stdin = FakeStream(b"")
        stdout = FakeStream(b"\x00" * 6400)  # 100 ms of 16 kHz mono

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    tts._proc = FakeProc()  # type: ignore[assignment]
    chunks = await _collect(tts.stream("hello", "default"))
    assert chunks, "must emit at least one AudioChunk"
    assert all(isinstance(c, AudioChunk) for c in chunks)
    assert all(c.dur_ms > 0 for c in chunks)

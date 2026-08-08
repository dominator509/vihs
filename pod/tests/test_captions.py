"""CaptionsChannel unit tests (EP-005 M4; SPEC-003 caption shape)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from vihs_pod.captions import CaptionsChannel, caption_frame


class FakeChannel:
    """Minimal RTCDataChannel stand-in (aiortc 1.15 sync send())."""

    def __init__(self) -> None:
        self.readyState = "open"
        self._handlers: dict[str, Callable[[Any], None]] = {}
        self.sent: list[str] = []

    def on(self, event: str, cb: Callable[[Any], None]) -> None:
        self._handlers[event] = cb

    def send(self, msg: str) -> None:
        self.sent.append(msg)


async def test_caption_frame_shape() -> None:
    f = caption_frame(42, "hi there", False)
    assert f == {"t": "caption", "turn_id": 42, "delta": "hi there", "final": False}
    f2 = caption_frame(0, "", True)
    assert f2["final"] is True


async def test_channel_send_serializes_and_receives() -> None:
    ch = FakeChannel()
    caps = CaptionsChannel(ch)

    await caps.send(1, "hello", True)
    assert json.loads(ch.sent[0]) == caption_frame(1, "hello", True)

    # inbound message (the client path / loopback) is decoded and queued.
    ch._handlers["message"](json.dumps(caption_frame(2, "inbound", False)))
    got = await caps.next(timeout=1.0)
    assert got == caption_frame(2, "inbound", False)


async def test_channel_ignores_non_json_noise() -> None:
    ch = FakeChannel()
    caps = CaptionsChannel(ch)
    ch._handlers["message"]("not json at all")
    ch._handlers["message"](b"\x00\x01binary")
    try:
        await caps.next(timeout=0.2)
        raise AssertionError("expected timeout on noise-only channel")
    except TimeoutError:
        pass


async def test_send_drops_when_channel_closed() -> None:
    ch = FakeChannel()
    ch.readyState = "closed"
    caps = CaptionsChannel(ch)
    await caps.send(1, "x", False)
    assert ch.sent == []

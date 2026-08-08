"""Captions data channel (SPEC-003): the WebRTC `captions` channel mirrors
the server→client `caption` message shape:

    {"t": "caption", "turn_id": 42, "delta": "...", "final": false}

The pod sends rendered deltas over the channel; the client renders them.
This module is transport-neutral: it wraps an RTCDataChannel for the live
path and is the same object the loopback proof uses (EP-005 M4).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


def caption_frame(turn_id: int, delta: str, final: bool) -> dict[str, Any]:
    """Build a SPEC-003 caption message."""
    return {"t": "caption", "turn_id": turn_id, "delta": delta, "final": final}


class CaptionsChannel:
    """Wraps an RTCDataChannel named `captions`.

    Incoming messages are JSON-decoded and queued in order; `send` mirrors
    the caption message shape. Callers await `next()` to receive the next
    caption (used by the loopback proof and by the live client relay).
    """

    def __init__(self, channel: Any) -> None:
        self._channel = channel
        self._received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        channel.on("message", self._on_message)

    def _on_message(self, msg: Any) -> None:
        try:
            text = msg.decode("utf-8") if isinstance(msg, bytes | bytearray) else str(msg)
            self._received.put_nowait(json.loads(text))
        except (ValueError, TypeError):
            pass  # non-JSON noise is dropped, never crashes the pipeline

    @property
    def open(self) -> bool:
        return bool(self._channel.readyState == "open")

    async def send(self, turn_id: int, delta: str, final: bool) -> None:
        """Send a caption delta if the channel is open; otherwise drop."""
        if self.open:
            self._channel.send(json.dumps(caption_frame(turn_id, delta, final)))

    async def next(self, timeout: float = 5.0) -> dict[str, Any]:
        """Wait for the next caption message (raises on timeout)."""
        return await asyncio.wait_for(self._received.get(), timeout)

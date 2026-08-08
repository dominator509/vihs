"""In-process WebRTC loopback proof (EP-005 M4).

Two RTCPeerConnections in one process exchange full (non-trickle) SDP.
aiortc 1.15 embeds the gathered ICE candidates in the local description, so
the offer/answer frames carry them — the same frame flow the orchestrator
relay carries between a real browser and the pod (SPEC-003 signaling). The
pod uses this proof on assignment to verify its media path works before
serving; M5's browser replaces the in-process client.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiortc import RTCPeerConnection, RTCSessionDescription

from .captions import CaptionsChannel, caption_frame

GATHER_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 8.0
CHANNEL_TIMEOUT_S = 5.0
CAPTION_TIMEOUT_S = 5.0


async def wait_gathering_complete(pc: RTCPeerConnection, timeout: float = GATHER_TIMEOUT_S) -> None:
    """Wait until the local description embeds all ICE candidates."""
    for _ in range(int(timeout / 0.05)):
        if pc.iceGatheringState == "complete":
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"ICE gathering not complete after {timeout}s (state={pc.iceGatheringState})"
    )


async def wait_connected(pc: RTCPeerConnection, timeout: float = CONNECT_TIMEOUT_S) -> None:
    for _ in range(int(timeout / 0.05)):
        if pc.connectionState == "connected":
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"WebRTC not connected after {timeout}s (state={pc.connectionState})")


async def wait_open(channel: Any, timeout: float = CHANNEL_TIMEOUT_S) -> None:
    for _ in range(int(timeout / 0.05)):
        if channel.readyState == "open":
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"data channel not open after {timeout}s (state={channel.readyState})")


async def run_loopback_proof(
    turn_id: int, delta: str = "loopback", timeout: float = CAPTION_TIMEOUT_S
) -> tuple[RTCPeerConnection, RTCPeerConnection]:
    """Prove the pod's WebRTC path: two in-process peers, captions round-trip.

    Returns (pc_pod, pc_client) so the caller holds the media path open
    until revoke. Raises on any step failing.
    """
    pc_pod = RTCPeerConnection()
    pc_client = RTCPeerConnection()
    pod_channel = pc_pod.createDataChannel("captions")
    received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    @pc_client.on("datachannel")
    def on_datachannel(channel: Any) -> None:
        if channel.label == "captions":
            channel.on("message", lambda msg: received.put_nowait(json.loads(str(msg))))

    # Pod offers; aiortc 1.15 embeds candidates after gathering completes.
    offer = await pc_pod.createOffer()
    await pc_pod.setLocalDescription(offer)
    await wait_gathering_complete(pc_pod)

    await pc_client.setRemoteDescription(
        RTCSessionDescription(sdp=pc_pod.localDescription.sdp, type="offer")
    )
    answer = await pc_client.createAnswer()
    await pc_client.setLocalDescription(answer)
    await wait_gathering_complete(pc_client)

    await pc_pod.setRemoteDescription(
        RTCSessionDescription(sdp=pc_client.localDescription.sdp, type="answer")
    )

    await wait_connected(pc_pod)
    await wait_connected(pc_client)
    await wait_open(pod_channel)

    caps = CaptionsChannel(pod_channel)
    await caps.send(turn_id=turn_id, delta=delta, final=False)
    got = await asyncio.wait_for(received.get(), timeout)
    expected = caption_frame(turn_id, delta, False)
    if got != expected:
        raise AssertionError(f"caption round-trip mismatch: got={got!r} expected={expected!r}")
    return pc_pod, pc_client

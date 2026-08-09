"""EP-007 M4 — pod signal routing by connection_id (GAP-M4-2).

With concurrent assignments on one pod (capacity ramp), each client's
signaling WS belongs to its OWN session. The pod's signal_handler must
route by the ?connection_id=… query param, NOT the single
`_conversation` pointer — otherwise every peer's offer lands in the
last-assigned conversation's bridge and the first peers never connect.

These tests exercise the real WS surface with two simultaneous
assignments and prove each client's frames reach its own bridge.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import websockets
from websockets.asyncio.server import Server

from vihs_pod.agent import PodAgent
from vihs_pod.health import SignalBridge, start_pod_surface


async def _two_assignment_fixture() -> tuple[Server, int, SignalBridge, SignalBridge]:
    """A pod surface whose agent holds two conversations with distinct
    connection_ids; returns (server, port, bridge_a, bridge_b)."""
    agent = PodAgent(
        pod_id="pod-test",
        pod_addr="127.0.0.1:0",
        orch_addr="127.0.0.1:8080",
        token="tok",
        cap=2,
    )
    bridge_a = SignalBridge()
    bridge_b = SignalBridge()
    convo_a: object = SimpleNamespace(connection_id="conn-a", bridge=bridge_a)
    convo_b: object = SimpleNamespace(connection_id="conn-b", bridge=bridge_b)
    # type: ignore[assignment] — SimpleNamespace stands in for Conversation
    # in these routing tests (only .connection_id/.bridge are touched).
    agent._assignments = {  # type: ignore[assignment]
        "sess-a": convo_a,
        "sess-b": convo_b,
    }
    agent._conversation = convo_b  # type: ignore[assignment]  # the LAST assigned
    server = await start_pod_surface("127.0.0.1", 0, "pod-test", agent.health, agent.signal_handler)
    port = server.sockets[0].getsockname()[1]
    return server, port, bridge_a, bridge_b


async def test_signal_routes_by_connection_id() -> None:
    server, port, bridge_a, bridge_b = await _two_assignment_fixture()
    try:
        ws_a = await websockets.connect(
            f"ws://127.0.0.1:{port}/internal/pods/pod-test/signal?connection_id=conn-a"
        )
        ws_b = await websockets.connect(
            f"ws://127.0.0.1:{port}/internal/pods/pod-test/signal?connection_id=conn-b"
        )
        try:
            await ws_a.send(json.dumps({"t": "offer", "sdp": "v=0 a"}))
            await ws_b.send(json.dumps({"t": "offer", "sdp": "v=0 b"}))

            got_a = await asyncio.wait_for(bridge_a.inbound.get(), timeout=5.0)
            got_b = await asyncio.wait_for(bridge_b.inbound.get(), timeout=5.0)
            assert got_a["sdp"] == "v=0 a", got_a
            assert got_b["sdp"] == "v=0 b", got_b
        finally:
            await ws_a.close()
            await ws_b.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_signal_unknown_connection_closed() -> None:
    """A signal WS for an unknown connection_id must be rejected (1008),
    not silently routed to the last-assigned conversation."""
    server, port, _a, _b = await _two_assignment_fixture()
    try:
        ws = await websockets.connect(
            f"ws://127.0.0.1:{port}/internal/pods/pod-test/signal?connection_id=nope"
        )
        try:
            await asyncio.wait_for(ws.recv(), timeout=5.0)
            raise AssertionError("expected close frame")
        except websockets.exceptions.ConnectionClosed as e:
            assert e.code == 1008, e.code
        finally:
            await ws.close()
    finally:
        server.close()
        await server.wait_closed()

"""Pod local surface tests (EP-005 M4): GET /health + signal WS routing."""

from __future__ import annotations

import asyncio
import json

import httpx
import websockets

from vihs_pod.health import SignalBridge, start_pod_surface


async def _start(health_fn, signal_handler):
    server = await start_pod_surface("127.0.0.1", 0, "pod-1", health_fn, signal_handler)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_health_endpoint() -> None:
    async def handler(conn: object) -> None:  # unused in this test
        return None

    health = {"stages": {"vad": "ready", "llm": "ready"}, "fill": 0, "cap": 2}
    server, port = await _start(lambda: health, handler)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/health")
            assert resp.status_code == 200
            assert resp.json() == health
            assert resp.headers["content-type"].startswith("application/json")
    finally:
        server.close()
        await server.wait_closed()


async def test_metrics_endpoint_serves_prometheus_text() -> None:
    """EP-008 M2: GET /metrics returns the Prometheus exposition."""

    async def handler(conn: object) -> None:  # unused in this test
        return None

    metrics_body = (
        "# HELP vihs_bargein_total Barge-in events.\n"
        "# TYPE vihs_bargein_total counter\n"
        'vihs_bargein_total{pod_id="pod-1",model_ver="mock"} 0\n'
    )
    server = await start_pod_surface(
        "127.0.0.1", 0, "pod-1", lambda: {}, handler, metrics_fn=lambda: metrics_body
    )
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/metrics")
            assert resp.status_code == 200
            assert resp.text == metrics_body
            assert "text/plain" in resp.headers["content-type"]
    finally:
        server.close()
        await server.wait_closed()


async def test_signal_ws_routes_to_handler() -> None:
    received: asyncio.Queue[str] = asyncio.Queue()

    async def handler(conn: websockets.asyncio.server.ServerConnection) -> None:
        async for raw in conn:
            await received.put(raw)
            await conn.send(f"echo:{raw}")

    server, port = await _start(lambda: {}, handler)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/internal/pods/pod-1/signal?connection_id=c1"
        ) as ws:
            await ws.send('{"t":"offer","sdp":"v=0"}')
            reply = await asyncio.wait_for(ws.recv(), timeout=5.0)
            assert reply == 'echo:{"t":"offer","sdp":"v=0"}'
            got = await asyncio.wait_for(received.get(), timeout=5.0)
            assert json.loads(got)["t"] == "offer"
    finally:
        server.close()
        await server.wait_closed()


async def test_signal_ws_wrong_path_closed() -> None:
    async def handler(conn: object) -> None:
        return None

    server, port = await _start(lambda: {}, handler)
    try:
        with_conn = await websockets.connect(
            f"ws://127.0.0.1:{port}/internal/pods/other/signal?connection_id=c1"
        )
        try:
            # Unknown path closes with 1008 quickly.
            await asyncio.wait_for(with_conn.recv(), timeout=5.0)
            raise AssertionError("expected close frame")
        except websockets.exceptions.ConnectionClosed as e:
            assert e.rcvd is None or e.rcvd.code == 1008 or e.code == 1008
        finally:
            await with_conn.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_signal_bridge_queues() -> None:
    bridge = SignalBridge()
    await bridge.send_to_pod({"t": "ice", "candidate": {"a": 1}})
    frame = await asyncio.wait_for(bridge.inbound.get(), timeout=1.0)
    assert frame["t"] == "ice"
    await bridge.outbound.put({"t": "answer", "sdp": "v=0"})
    out = await bridge.next_to_client(timeout=1.0)
    assert out["t"] == "answer"

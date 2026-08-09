"""Pod local surface (SPEC-003): GET /health + WS /internal/pods/{id}/signal.

The orchestrator connects to the pod-ward signal socket
(`ws://{pod_addr}/internal/pods/{pod_id}/signal?connection_id=…`) to relay
client SDP/ICE; the same handler pattern is exercised in-process by the
EP-005 M4 loopback proof via `SignalBridge`.

websockets 17 `process_request` answers the plain-HTTP health probe; the WS
handler routes signal frames to the active assignment's bridge.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Response

HealthFn = Callable[[], dict[str, Any]]
MetricsFn = Callable[[], str]
SignalHandler = Callable[[ServerConnection], Awaitable[None]]


def _json_response(health: dict[str, Any]) -> Response:
    body = json.dumps(health).encode("utf-8")
    return Response(200, "OK", Headers({"Content-Type": "application/json"}), body)


def _text_response(
    body: str,
    content_type: str = "text/plain; version=0.0.4; charset=utf-8",
) -> Response:
    return Response(200, "OK", Headers({"Content-Type": content_type}), body.encode("utf-8"))


class SignalBridge:
    """Queue pair a signal transport (WS or in-process loopback) bridges to.

    - `inbound`: frames arriving from the client side (offer/ice) → the pod
      applies them to its RTCPeerConnection.
    - `outbound`: frames the pod wants the client to see (answer/ice) → the
      transport drains them.
    The loopback proof feeds these queues in-process; the signal WS handler
    feeds them over the orchestrator relay. Same code path.
    """

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send_to_pod(self, frame: dict[str, Any]) -> None:
        await self.inbound.put(frame)

    async def next_to_client(self, timeout: float = 5.0) -> dict[str, Any]:
        return await asyncio.wait_for(self.outbound.get(), timeout)


async def start_pod_surface(
    host: str,
    port: int,
    pod_id: str,
    health_fn: HealthFn,
    signal_handler: SignalHandler,
    metrics_fn: MetricsFn | None = None,
) -> websockets.asyncio.server.Server:
    """Start the pod's local HTTP+WS surface; returns the running server.

    - GET /health → {stages:{...}, fill, cap} (SPEC-003 pod local surface)
    - GET /metrics → Prometheus text exposition (SPEC-007 O4; EP-008 M2)
    - WS /internal/pods/{id}/signal?connection_id=… → `signal_handler`
    """

    def process_request(conn: ServerConnection, request: Any) -> Response | None:
        path = request.path.split("?", 1)[0]
        if path == "/health":
            return _json_response(health_fn())
        if path == "/metrics" and metrics_fn is not None:
            return _text_response(metrics_fn())
        return None

    async def ws_handler(conn: ServerConnection) -> None:
        path = conn.request.path if conn.request is not None else ""
        base = path.split("?", 1)[0]
        if base == f"/internal/pods/{pod_id}/signal":
            await signal_handler(conn)
        else:
            await conn.close(code=1008, reason="unknown path")

    return await websockets.serve(
        ws_handler,
        host,
        port,
        process_request=process_request,
        max_size=32 * 1024,
    )


async def serve_pod_surface(
    host: str,
    port: int,
    pod_id: str,
    health_fn: HealthFn,
    signal_handler: SignalHandler,
    metrics_fn: MetricsFn | None = None,
) -> None:
    """Run the pod's local HTTP+WS surface until cancelled."""
    server = await start_pod_surface(host, port, pod_id, health_fn, signal_handler, metrics_fn)
    try:
        await asyncio.Future()  # run forever until cancelled
    finally:
        server.close()
        await server.wait_closed()

"""Pod agent bootstrap (EP-005 M4; SPEC-003 pod rows).

Boot order:
  1. pod local surface (GET /health + WS /internal/pods/{id}/signal) up
  2. POST /internal/pods/register
  3. health ping loop (5 s)
  4. WS /internal/pods/{id}/assign — ready/ack, then assign/revoke frames

On assignment the pod runs the in-process WebRTC loopback proof + captions
round-trip (mock stages in CI; real stages behind VIHS_REAL_STAGES=1 land
in M7). The signal WS handler routes client signaling frames to the active
assignment's SignalBridge (driven by the browser in M5).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from typing import Any

import httpx
import websockets
from websockets.asyncio.server import ServerConnection

from vihs_pod.health import SignalBridge, serve_pod_surface
from vihs_pod.webrtc_loopback import run_loopback_proof

log = logging.getLogger("vihs_pod.agent")

DEFAULT_ORCH = "127.0.0.1:8080"
DEFAULT_POD_ADDR = "127.0.0.1:8093"
DEFAULT_TOKEN = "dev-pod-token"
HEALTH_INTERVAL_S = 5.0
STAGES = ("vad", "stt", "llm", "tts", "lipsync", "mux")


def _http_base(addr: str) -> str:
    host, _, port = addr.partition(":")
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    return f"http://{host}:{port or 8080}"


def _assign_ws_url(addr: str, pod_id: str) -> str:
    return _http_base(addr).replace("http://", "ws://") + f"/internal/pods/{pod_id}/assign"


class PodAgent:
    def __init__(
        self,
        pod_id: str,
        pod_addr: str,
        orch_addr: str,
        token: str,
        cap: int,
        real_stages: bool = False,
    ) -> None:
        self.pod_id = pod_id
        self.pod_addr = pod_addr
        self.orch_addr = orch_addr
        self.token = token
        self.cap = max(1, cap)
        self.real_stages = real_stages
        self._assignments: dict[str, tuple[Any, Any]] = {}
        self._signal_bridge: SignalBridge | None = None

    # --- pod local surface ---

    def health(self) -> dict[str, Any]:
        mode = "real" if self.real_stages else "mock"
        return {
            "stages": {s: "ready" for s in STAGES},
            "stages_mode": mode,
            "fill": len(self._assignments),
            "cap": self.cap,
        }

    async def signal_handler(self, conn: ServerConnection) -> None:
        """WS /internal/pods/{id}/signal — relay client frames to the active
        assignment's SignalBridge and drain pod→client frames back."""
        bridge = self._signal_bridge
        if bridge is None:
            await conn.close(code=1008, reason="no active assignment")
            return

        async def writer() -> None:
            while True:
                frame = await bridge.next_to_client()
                await conn.send(json.dumps(frame))

        writer_task = asyncio.create_task(writer())
        try:
            async for raw in conn:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                await bridge.send_to_pod(frame)
        finally:
            writer_task.cancel()

    # --- lifecycle ---

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def register(self) -> None:
        body = {
            "pod_id": self.pod_id,
            "addr": self.pod_addr,
            "cap": self.cap,
            "versions": {"pod": "0.1.0", "stages": "mock" if not self.real_stages else "real"},
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{_http_base(self.orch_addr)}/internal/pods/register",
                json=body,
                headers=self.bearer,
            )
            resp.raise_for_status()
            log.info("registered: %s", resp.json().get("assign_ws"))

    async def health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_INTERVAL_S)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(
                        f"{_http_base(self.orch_addr)}/internal/pods/{self.pod_id}/health",
                        json=self.health(),
                        headers=self.bearer,
                    )
                    resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 — health is best-effort
                log.warning("health ping failed: %s", exc)

    async def assign_loop(self) -> None:
        url = _assign_ws_url(self.orch_addr, self.pod_id)
        log.info("connecting assign WS %s", url)
        async with websockets.connect(
            url, additional_headers=self.bearer, max_size=32 * 1024
        ) as ws:
            await ws.send(json.dumps({"t": "ready"}))
            async for raw in ws:
                frame = json.loads(raw)
                t = frame.get("t")
                if t == "ack":
                    log.info("assign channel live")
                elif t == "assign":
                    await self.handle_assign(frame)
                elif t == "revoke":
                    await self.handle_revoke(frame)

    async def handle_assign(self, frame: dict[str, Any]) -> None:
        session_id = frame["session_id"]
        connection_id = frame.get("connection_id", "")
        resume = bool(frame.get("resume", False))
        cursor = frame.get("cursor", {}) or {}
        log.info(
            "pod assigned session=%s connection=%s resume=%s",
            session_id,
            connection_id,
            resume,
        )
        self._signal_bridge = SignalBridge()
        turn_id = int(cursor.get("last_turn_id", 0)) + 1
        pc_pod, pc_client = await run_loopback_proof(turn_id=turn_id)
        self._assignments[session_id] = (pc_pod, pc_client)
        log.info("captions loopback ok session=%s", session_id)

    async def handle_revoke(self, frame: dict[str, Any]) -> None:
        session_id = frame.get("session_id", "")
        if session_id in self._assignments:
            pc_pod, pc_client = self._assignments.pop(session_id)
            await pc_pod.close()
            await pc_client.close()
            log.info("assignment revoked session=%s", session_id)
        self._signal_bridge = None

    async def run(self) -> None:
        host, _, port = self.pod_addr.partition(":")
        surface_task = asyncio.create_task(
            serve_pod_surface(
                host or "127.0.0.1",
                int(port or 8093),
                self.pod_id,
                self.health,
                self.signal_handler,
            )
        )
        try:
            await self.register()
            health_task = asyncio.create_task(self.health_loop())
            try:
                await self.assign_loop()
            finally:
                health_task.cancel()
        finally:
            surface_task.cancel()
            await asyncio.gather(surface_task, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vihs-pod")
    parser.add_argument(
        "--mock-gpu",
        action="store_true",
        help="run with mock stage implementations (CI-safe)",
    )
    parser.add_argument("--pod-id", default=os.environ.get("VIHS_POD_ID", "local-pod"))
    parser.add_argument("--addr", default=os.environ.get("VIHS_POD_ADDR", DEFAULT_POD_ADDR))
    parser.add_argument("--orch", default=os.environ.get("VIHS_ORCH_ADDR", DEFAULT_ORCH))
    parser.add_argument("--token", default=os.environ.get("VIHS_POD_TOKEN", DEFAULT_TOKEN))
    parser.add_argument(
        "--cap",
        type=int,
        default=int(os.environ.get("POD_MAX_SESSIONS", "2")),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("VIHS_POD_LOG", "info").upper(),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    real_stages = os.environ.get("VIHS_REAL_STAGES", "0") == "1"
    agent = PodAgent(
        pod_id=args.pod_id,
        pod_addr=args.addr,
        orch_addr=args.orch,
        token=args.token,
        cap=args.cap,
        real_stages=real_stages,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(agent)))
    try:
        loop.run_until_complete(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
    return 0


async def _shutdown(agent: PodAgent) -> None:
    log.info("shutting down")
    for pc_pod, pc_client in agent._assignments.values():
        await pc_pod.close()
        await pc_client.close()
    agent._assignments.clear()
    asyncio.get_event_loop().stop()


if __name__ == "__main__":
    sys.exit(main())

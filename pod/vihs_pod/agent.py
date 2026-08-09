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
import urllib.parse
from typing import Any

import httpx
import websockets
from websockets.asyncio.server import ServerConnection

from vihs_pod.conversation import Conversation, build_stages
from vihs_pod.health import serve_pod_surface
from vihs_pod.memory_client import MemoryClient
from vihs_pod.metrics import Metrics

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
        memoryd_addr: str = "127.0.0.1:8091",
        mock_answers: list[str] | None = None,
    ) -> None:
        self.pod_id = pod_id
        self.pod_addr = pod_addr
        self.orch_addr = orch_addr
        self.token = token
        self.cap = max(1, cap)
        self.real_stages = real_stages
        self.memoryd_addr = memoryd_addr
        self.mock_answers = mock_answers or []
        self._assignments: dict[str, Conversation] = {}
        self._conversation: Conversation | None = None

    # --- pod local surface ---

    def health(self) -> dict[str, Any]:
        mode = "real" if self.real_stages else "mock"
        convo = self._conversation
        # Aggregate per-stage first-chunk histograms across ALL assignments
        # (SPEC-007 O1: the histogram is labeled pod_id — this is the pod's
        # view). The capacity harness (EP-007 M4) reads these via /health.
        convos = list(self._assignments.values())
        metrics_report: dict[str, Any] = {}
        if convos:
            agg = Metrics.aggregate([c.metrics for c in convos])
            metrics_report = agg.report()
        return {
            "stages": {s: "ready" for s in STAGES},
            "stages_mode": mode,
            "fill": len(self._assignments),
            "cap": self.cap,
            # SPEC-006 row 1: any assignment with 2+ stage crashes marks the
            # pod degraded; the orchestrator drains it (stops new assigns).
            "degraded": any(c.degraded for c in self._assignments.values()),
            "stage_crashes": sum(c.stage_crashes for c in self._assignments.values()),
            # R2 append buffer visibility (EP-007 M2): the chaos suite asserts
            # depth rises while memoryd is paused, then drains to 0.
            "append_buffer_depth": convo.append_buffer.depth if convo else 0,
            "append_buffer_queued": convo.append_buffer.queued if convo else 0,
            "append_buffer_in_flight": convo.append_buffer.in_flight if convo else 0,
            "append_buffer_flusher_alive": convo.append_buffer.flusher_alive if convo else False,
            "append_buffer_degraded": convo.append_buffer.degraded if convo else False,
            # Per-stage first-chunk latency percentiles (SPEC-007 O1).
            "metrics": metrics_report,
        }

    async def signal_handler(self, conn: ServerConnection) -> None:
        """WS /internal/pods/{id}/signal?connection_id=… — relay client
        frames to the conversation OWNING that connection and drain
        pod→client frames back.

        EP-007 M4: route by connection_id, NOT the single `_conversation`
        pointer — with concurrent assignments (capacity ramp) each signal
        WS belongs to its own session; using `_conversation` would feed
        every peer's offer into the last-assigned conversation's bridge.
        """
        # The orchestrator includes ?connection_id=… on the pod-ward WS.
        path = conn.request.path if conn.request is not None else ""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        connection_id = (qs.get("connection_id") or [""])[0]
        convo = next(
            (c for c in self._assignments.values() if c.connection_id == connection_id),
            None,
        )
        if convo is None:
            await conn.close(code=1008, reason="no active assignment")
            return
        bridge = convo.bridge

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
        pod_token = frame.get("pod_token", self.token)
        log.info(
            "pod assigned session=%s connection=%s resume=%s",
            session_id,
            connection_id,
            resume,
        )
        memory = MemoryClient(f"http://{self.memoryd_addr}", pod_token=pod_token)
        stages, monitor = build_stages(self.mock_answers, real=self.real_stages)
        convo = Conversation(
            session_id=session_id,
            connection_id=connection_id,
            pod_token=pod_token,
            cursor=cursor,
            memory=memory,
            stages=stages,
            monitor=monitor,
        )
        await convo.start()
        self._conversation = convo
        self._assignments[session_id] = convo
        log.info("conversation ready session=%s", session_id)

    async def handle_revoke(self, frame: dict[str, Any]) -> None:
        session_id = frame.get("session_id", "")
        convo = self._assignments.pop(session_id, None)
        if convo is not None:
            await convo.stop()
            log.info("assignment revoked session=%s", session_id)
        if self._conversation is convo:
            self._conversation = None

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


def _parse_mock_answers(raw: str | None) -> list[str]:
    """VIHS_MOCK_ANSWERS: JSON array of per-turn scripted answers (mock LLM)."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return list(parsed) if isinstance(parsed, list) else []
    except ValueError:
        return []


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
        memoryd_addr=os.environ.get("VIHS_MEMORYD_ADDR", "127.0.0.1:8091"),
        mock_answers=_parse_mock_answers(os.environ.get("VIHS_MOCK_ANSWERS")),
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
    for convo in list(agent._assignments.values()):
        await convo.stop()
    agent._assignments.clear()
    asyncio.get_event_loop().stop()


if __name__ == "__main__":
    sys.exit(main())

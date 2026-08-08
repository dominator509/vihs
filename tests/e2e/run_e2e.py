#!/usr/bin/env python3
"""VIHS E2E harness (EP-005 M4/M5; SPEC-001/SPEC-004 acceptance).

Targets:
  e2e_connect  — pod register → health → assign WS → conversation ready (M4 boot).
  e2e_convo    — full scripted conversation through the REAL relay: client
                 aiortc peer ↔ orchestrator signal WS ↔ pod; live captions;
                 barge-in commits the INV-1 played prefix (M5).
  e2e_resume   — same session, disconnect, resume: durable cursor drives
                 resume=true; context continues (M5).
  (default)    — runs all three; the verify.sh E2E gate.

The harness creates and deletes its own sessions (EP-005 §11). Services it
starts itself are killed on exit; services already running are left alone.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from aiortc import RTCPeerConnection, RTCSessionDescription

from vihs_pod.webrtc_loopback import wait_gathering_complete

ROOT = Path(__file__).resolve().parents[2]
POD_VENV = ROOT / "pod" / ".venv" / "bin" / "python"
ORCH_ADDR = "127.0.0.1:8080"
MEMORYD_ADDR = "127.0.0.1:8091"
POD_ADDR = "127.0.0.1:8093"
USER_TOKEN = "dev-e2e-user-token"
POD_TOKEN = "dev-pod-token"
WAIT = 20.0

ANSWER_LONG = "First sentence is done. Second one is longer and gets cut before finishing."
ANSWER_SHORT = "Understood. Continuing from here."
# Turn 1 answers short; turn 2 is the long barge-in target; turn 3 uses the
# ScriptedLLM fallback ("Understood.").
MOCK_ANSWERS = [ANSWER_SHORT, ANSWER_LONG]


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def http_get(url: str, timeout: float = 2.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return -1


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def wait_http(url: str, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if http_get(url) == 200:
            return True
        time.sleep(0.25)
    return False


async def wait_for_text(path: Path, pattern: str, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(errors="replace")
            if pattern in text:
                return True
        await asyncio.sleep(0.25)
    return False


def ensure_services() -> list[subprocess.Popen]:
    """Start memoryd/orchestrator if not reachable; return PIDs we own."""
    started: list[subprocess.Popen] = []
    env = {**os.environ, **load_env(ROOT / ".env")}

    if not wait_http(f"http://{MEMORYD_ADDR}/healthz", timeout=3.0):
        print(f"  starting memoryd ({MEMORYD_ADDR})")
        started.append(
            subprocess.Popen(
                [str(ROOT / "target" / "debug" / "memoryd")],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        if not wait_http(f"http://{MEMORYD_ADDR}/healthz"):
            raise RuntimeError("memoryd did not become healthy")

    if not wait_http(f"http://{ORCH_ADDR}/healthz", timeout=3.0):
        print(f"  starting orchestrator ({ORCH_ADDR})")
        started.append(
            subprocess.Popen(
                [str(ROOT / "target" / "debug" / "orchestrator")],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        if not wait_http(f"http://{ORCH_ADDR}/healthz"):
            raise RuntimeError("orchestrator did not become healthy")

    if not (port_open("127.0.0.1", 6379) and port_open("127.0.0.1", 9000)):
        print("  starting dev services (redis + minio)")
        subprocess.run(
            ["sh", "scripts/dev-services.sh"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if port_open("127.0.0.1", 6379) and port_open("127.0.0.1", 9000):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("redis/minio did not become reachable")

    return started


def stop_processes(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + 5
    for p in procs:
        try:
            p.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()


def api(
    path: str, method: str = "GET", body: dict | None = None, token: str = USER_TOKEN
) -> tuple[int, dict]:
    url = f"http://{ORCH_ADDR}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {}


def transcript_of(session_id: str) -> str:
    _, body = api(f"/v1/sessions/{session_id}/transcript")
    return body if isinstance(body, str) else json.dumps(body)


def _start_pod_agent(pod_id: str, mock_answers: list[str] | None = None) -> tuple[subprocess.Popen, Path]:
    log_path = Path(tempfile.gettempdir()) / f"vihs-pod-{os.getpid()}.log"
    pod_env = {
        **os.environ,
        **load_env(ROOT / ".env"),
        "VIHS_POD_ADDR": POD_ADDR,
        "VIHS_POD_TOKEN": POD_TOKEN,
        "VIHS_POD_LOG": "info",
    }
    if mock_answers:
        pod_env["VIHS_MOCK_ANSWERS"] = json.dumps(mock_answers)
    cmd = [
        str(POD_VENV),
        "-m",
        "vihs_pod.agent",
        "--mock-gpu",
        "--pod-id",
        pod_id,
        "--addr",
        POD_ADDR,
        "--orch",
        ORCH_ADDR,
        "--token",
        POD_TOKEN,
    ]
    with open(log_path, "w") as logf:
        proc = subprocess.Popen(
            cmd, cwd=ROOT / "pod", env=pod_env, stdout=logf, stderr=subprocess.STDOUT
        )
    return proc, log_path


def _connect_session(session_id: str) -> str:
    status, body = api(f"/v1/sessions/{session_id}/connect", method="POST")
    if status != 200:
        raise RuntimeError(f"connect session failed: {status} {body}")
    return body["connect"]["connection_id"]


class ClientPeer:
    """aiortc client through the orchestrator relay (acts as the browser)."""

    def __init__(self, connection_id: str) -> None:
        self.connection_id = connection_id
        self.pc = RTCPeerConnection()
        self.captions: list[dict] = []
        self._captions_evt = asyncio.Event()
        self.captions_ch = self.pc.createDataChannel("captions")
        self.user_ch = self.pc.createDataChannel("user_input")
        self.captions_ch.on("message", self._on_caption)
        self.ws = None

    def _on_caption(self, msg: object) -> None:
        try:
            self.captions.append(json.loads(str(msg)))
            self._captions_evt.set()
        except ValueError:
            pass

    async def connect(self, timeout: float = 15.0) -> None:
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        await wait_gathering_complete(self.pc)
        self.ws = await asyncio.wait_for(
            websockets_connect(f"ws://{ORCH_ADDR}/v1/signal/{self.connection_id}"),
            timeout,
        )
        first = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
        if first.get("t") != "state":
            raise RuntimeError(f"expected state frame, got {first}")
        await self.ws.send(json.dumps({"t": "offer", "sdp": self.pc.localDescription.sdp}))
        while True:
            frame = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            if frame.get("t") == "answer":
                await self.pc.setRemoteDescription(
                    RTCSessionDescription(sdp=frame["sdp"], type="answer")
                )
                break
        for _ in range(int(timeout / 0.05)):
            if self.pc.connectionState == "connected":
                break
            await asyncio.sleep(0.05)
        if self.pc.connectionState != "connected":
            raise RuntimeError(f"webrtc not connected: {self.pc.connectionState}")
        for _ in range(100):
            if (
                self.captions_ch.readyState == "open"
                and self.user_ch.readyState == "open"
            ):
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("data channels did not open")

    async def say(self, text: str) -> None:
        self.user_ch.send(json.dumps({"t": "user_input", "text": text}))

    async def wait_caption(self, timeout: float = 10.0) -> None:
        self._captions_evt.clear()
        await asyncio.wait_for(self._captions_evt.wait(), timeout)

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        await self.pc.close()


def websockets_connect(url: str):
    import websockets

    return websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {USER_TOKEN}"}, max_size=64 * 1024
    )


def e2e_connect() -> None:
    print("== e2e_connect: pod register/health/assign + conversation ready ==")
    owned = ensure_services()
    pod_proc: subprocess.Popen | None = None
    session_id: str | None = None
    try:
        pod_id = f"e2e-pod-{os.getpid()}"
        pod_proc, log_path = _start_pod_agent(pod_id)

        if not asyncio.run(wait_for_text(log_path, "assign channel live", timeout=15.0)):
            raise RuntimeError("pod did not connect its assign WS (register/ack failed)")

        status, body = api("/v1/sessions", method="POST", body={"persona_id": "e2e"})
        if status != 201:
            raise RuntimeError(f"create session failed: {status} {body}")
        session_id = body["session_id"]
        assert isinstance(session_id, str)

        _connect_session(session_id)

        if not asyncio.run(wait_for_text(log_path, "conversation ready", timeout=10.0)):
            raise RuntimeError("pod did not start the assignment conversation")

        status = http_get(f"http://{POD_ADDR}/health")
        if status != 200:
            raise RuntimeError(f"pod /health returned {status}")
        print("  register → assign → conversation ready → /health OK")
        print("e2e_connect OK")
    finally:
        if session_id:
            api(f"/v1/sessions/{session_id}", method="DELETE")
        if pod_proc is not None and pod_proc.poll() is None:
            pod_proc.send_signal(signal.SIGTERM)
            try:
                pod_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pod_proc.kill()
        stop_processes(owned)


async def _run_convo_target(target: str) -> None:
    owned = ensure_services()
    pod_proc: subprocess.Popen | None = None
    session_id: str | None = None
    try:
        pod_id = f"e2e-{target}-{os.getpid()}"
        pod_proc, log_path = _start_pod_agent(pod_id, MOCK_ANSWERS)

        if not await wait_for_text(log_path, "assign channel live", timeout=15.0):
            raise RuntimeError("pod assign WS not live")

        status, body = api("/v1/sessions", method="POST", body={"persona_id": "e2e"})
        if status != 201:
            raise RuntimeError(f"create session failed: {status} {body}")
        session_id = body["session_id"]
        assert isinstance(session_id, str)

        connection_id = _connect_session(session_id)
        if not await wait_for_text(log_path, "conversation ready", timeout=10.0):
            raise RuntimeError("pod conversation not ready")
        print(f"  session {session_id[:8]}… connected; pod conversation ready")

        peer = ClientPeer(connection_id)
        try:
            await peer.connect()
            print("  client WebRTC connected through relay (captions + user_input open)")

            if target == "e2e_convo":
                await _drive_convo(peer, pod_proc, log_path, session_id)
            else:
                await _drive_resume(peer, pod_proc, log_path, session_id)
        finally:
            await peer.close()

        print(f"{target} OK")
    finally:
        if session_id:
            api(f"/v1/sessions/{session_id}", method="DELETE")
        if pod_proc is not None and pod_proc.poll() is None:
            pod_proc.send_signal(signal.SIGTERM)
            try:
                pod_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pod_proc.kill()
        stop_processes(owned)


async def _drive_convo(peer: ClientPeer, pod_proc: subprocess.Popen, log_path: Path, session_id: str) -> None:
    # Turn 1: short normal answer (ANSWER_SHORT).
    await peer.say("Hello there.")
    await peer.wait_caption(timeout=10.0)
    if not await wait_for_text(log_path, "committed turn=1 interrupted=False", timeout=10.0):
        raise RuntimeError("turn 1 did not commit normally")
    transcript = transcript_of(session_id)
    if ANSWER_SHORT not in transcript:
        raise RuntimeError(f"turn 1 answer missing from transcript:\n{transcript}")

    # Turn 2: LONG answer (barge-in target). Interrupt it mid-clause-2.
    await peer.say("Wait, actually...")
    await peer.wait_caption(timeout=10.0)  # clause 1 delta (playback started)
    await peer.wait_caption(timeout=10.0)  # clause 2 delta (queued for play)
    await asyncio.sleep(0.30)  # clause 2 mid-playback (clause 2 plays ~510 ms)
    await peer.say("Never mind, continue.")
    if not await wait_for_text(log_path, "committed turn=2 interrupted=True", timeout=10.0):
        raise RuntimeError("barge-in did not commit an interrupted turn")

    # INV-1: the committed text is the PLAYED prefix — clause 1 verbatim plus
    # a proportional cut of clause 2; the unplayed tail must be absent.
    transcript = transcript_of(session_id)
    if "First sentence is done." not in transcript:
        raise RuntimeError(f"barge-in played prefix missing:\n{transcript}")
    if "before finishing." in transcript:
        raise RuntimeError(f"barge-in committed UNPLAYED text:\n{transcript}")

    # Turn 3: the follow-up input gets the ScriptedLLM fallback.
    if not await wait_for_text(log_path, "committed turn=3 interrupted=False", timeout=10.0):
        raise RuntimeError("post-barge-in answer did not commit")
    transcript = transcript_of(session_id)
    if "Understood." not in transcript:
        raise RuntimeError(f"post-barge-in answer missing from transcript:\n{transcript}")
    finals = [c for c in peer.captions if c.get("final")]
    if not finals:
        raise RuntimeError("no final:true caption frame observed")
    print("  convo: 3 turns committed; barge-in committed ONLY the played prefix; captions final seen")


async def _drive_resume(peer: ClientPeer, pod_proc: subprocess.Popen, log_path: Path, session_id: str) -> None:
    # Turn 1 on the first connection.
    await peer.say("First message before disconnect.")
    if not await wait_for_text(log_path, "committed turn=1 interrupted=False", timeout=10.0):
        raise RuntimeError("turn 1 did not commit")
    # Disconnect: close the client peer + signal WS. The assignment persists.
    await peer.close()

    # Resume the SAME session: durable cursor must drive resume=true.
    status, body = api(f"/v1/sessions/{session_id}/resume", method="POST")
    print("  resume status:", status, "body:", json.dumps(body)[:160])
    if status != 200:
        raise RuntimeError(f"resume failed: {status} {body}")
    connection_id = body["connect"]["connection_id"]
    await asyncio.sleep(0.5)
    recent = "\n".join(
        l for l in log_path.read_text(errors="replace").splitlines() if "assigned" in l
    )
    print("  pod assign lines after resume:\n" + recent)
    if not await wait_for_text(log_path, "resume=True", timeout=10.0):
        raise RuntimeError("resume frame did not carry resume=True")

    peer2 = ClientPeer(connection_id)
    try:
        await peer2.connect()
        await peer2.say("Second message after resume.")
        if not await wait_for_text(log_path, "committed turn=2 interrupted=False", timeout=10.0):
            raise RuntimeError("resumed turn 2 did not commit")
    finally:
        await peer2.close()

    transcript = transcript_of(session_id)
    if "First message before disconnect." not in transcript:
        raise RuntimeError("turn 1 missing after resume")
    if "Second message after resume." not in transcript:
        raise RuntimeError("turn 2 missing after resume")
    first = transcript.index("First message before disconnect.")
    second = transcript.index("Second message after resume.")
    if second < first:
        raise RuntimeError("turn order broken after resume")
    print("  resume carried resume=True; both turns present in order")


def main(argv: list[str] | None = None) -> int:
    targets = (argv or sys.argv[1:] or ["e2e_connect", "e2e_convo", "e2e_resume"])
    try:
        for target in targets:
            if target == "e2e_connect":
                e2e_connect()
            elif target in ("e2e_convo", "e2e_resume"):
                asyncio.run(_run_convo_target(target))
            else:
                print(f"unknown e2e target: {target}", file=sys.stderr)
                return 2
    except Exception as exc:  # noqa: BLE001
        print(f"e2e FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

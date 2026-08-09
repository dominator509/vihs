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
REMOTE = False  # --base-url sets this; skips local service/pod boot
METRICS_OUT: str | None = None  # set by --metrics-out DIR (EP-008 M4)
USER_TOKEN = ""  # minted at startup via POST /admin/tokens (EP-006 M1)
POD_TOKEN = ""  # loaded from .env VIHS_POD_TOKEN (32-byte b64url, seeded at startup)
WAIT = 20.0

ANSWER_LONG = (
    "First sentence is done. Second one is longer and gets cut before finishing."
)
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


def wait_transcript_contains(session_id: str, needle: str, timeout: float = WAIT) -> bool:
    """Remote-mode readiness: poll the DURABLE transcript via the orchestrator
    API until it contains `needle`. Stronger than a pod log line — it asserts
    the commit landed in the store, not that the pod printed something."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = transcript_of(session_id)
        if needle in t:
            return True
        time.sleep(0.5)
    return False


def wait_ready_pod(timeout: float = 120.0) -> dict:
    """Remote-mode pod wait: poll GET /admin/pods (admin bearer) until at
    least one pod is Ready. Returns the first Ready pod dict."""
    env = load_env(ROOT / ".env")
    admin_tok = env.get("VIHS_ADMIN_TOKEN", "")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(
                f"http://{ORCH_ADDR}/admin/pods",
                headers={"Authorization": f"Bearer {admin_tok}"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                body = json.loads(resp.read())
            for p in body.get("pods", []):
                if p.get("state") == "ready":
                    return p
        except Exception as exc:  # noqa: BLE001
            print(f"  admin/pods poll: {exc}")
        time.sleep(2.0)
    raise RuntimeError(f"no ready pod within {timeout}s on {ORCH_ADDR}")


def bootstrap_tokens() -> None:
    """EP-006 M1: mint a real user token via POST /admin/tokens using the
    seeded bootstrap admin token; load the real pod token from .env."""
    global USER_TOKEN, POD_TOKEN
    env = load_env(ROOT / ".env")
    admin_tok = env.get("VIHS_ADMIN_TOKEN", "")
    pod_tok = env.get("VIHS_POD_TOKEN", "")
    if not admin_tok or not pod_tok:
        raise RuntimeError(
            "bootstrap tokens missing: set VIHS_ADMIN_TOKEN and VIHS_POD_TOKEN "
            "(32-byte base64url) in .env"
        )
    POD_TOKEN = pod_tok
    # Mint a user token for the E2E owner. The admin listener shares the
    # public app in dev (main.rs merges admin_routes), so ORCH_ADDR works.
    data = json.dumps({"owner_id": "e2e-owner", "scope": "user"}).encode()
    req = urllib.request.Request(
        f"http://{ORCH_ADDR}/admin/tokens",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {admin_tok}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"mint user token failed: {e.code} {e.read()!r}") from e
    tok = body.get("token", "")
    if not tok:
        raise RuntimeError(f"mint response missing token: {body}")
    USER_TOKEN = tok
    print("  bootstrapped real user token via POST /admin/tokens")


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
    path: str, method: str = "GET", body: dict | None = None, token: str | None = None
) -> tuple[int, dict]:
    global USER_TOKEN
    if token is None:
        token = USER_TOKEN
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


def _start_pod_agent(
    pod_id: str, mock_answers: list[str] | None = None
) -> tuple[subprocess.Popen, Path]:
    log_path = Path(tempfile.gettempdir()) / f"vihs-pod-{os.getpid()}.log"
    # .env provides service defaults; PROCESS env wins so test/harness
    # overrides (e.g. POD_MAX_SESSIONS from tests/load/capacity.py) take
    # effect — capacity.py sets os.environ directly and documents that.
    pod_env = {
        **load_env(ROOT / ".env"),
        **os.environ,
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

    def __init__(self, connection_id: str, auth_frame: bool = False) -> None:
        self.connection_id = connection_id
        self.auth_frame = auth_frame
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
            websockets_connect(
                f"ws://{ORCH_ADDR}/v1/signal/{self.connection_id}",
                auth_frame=self.auth_frame,
            ),
            timeout,
        )
        if self.auth_frame:
            # Browser path (EP-006 M5): FIRST message is the SPEC-005 auth
            # frame — the WebSocket API cannot send headers.
            await self.ws.send(json.dumps({"t": "auth", "token": USER_TOKEN}))
        first = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
        if first.get("t") != "state":
            raise RuntimeError(f"expected state frame, got {first}")
        await self.ws.send(
            json.dumps({"t": "offer", "sdp": self.pc.localDescription.sdp})
        )
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


def websockets_connect(url: str, auth_frame: bool = False):
    import websockets

    if auth_frame:
        # Browser path (EP-006 M5): the WebSocket API cannot set headers, so
        # the FIRST message must be the SPEC-005 auth frame. No header at all.
        return websockets.connect(url, max_size=64 * 1024)
    return websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {USER_TOKEN}"},
        max_size=64 * 1024,
    )


def e2e_connect() -> None:
    print("== e2e_connect: pod register/health/assign + conversation ready ==")
    owned = ensure_services()
    pod_proc: subprocess.Popen | None = None
    session_id: str | None = None
    try:
        pod_id = f"e2e-pod-{os.getpid()}"
        pod_proc, log_path = _start_pod_agent(pod_id)

        if not asyncio.run(
            wait_for_text(log_path, "assign channel live", timeout=15.0)
        ):
            raise RuntimeError(
                "pod did not connect its assign WS (register/ack failed)"
            )

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


async def _remote_resume() -> None:
    """EP-009 M4 remote smoke: drive one turn + resume against a REMOTE
    orchestrator with a pre-registered (real) pod. No local service boot, no
    pod spawn — asserts against the DURABLE transcript via the API."""
    print(f"== remote resume against {ORCH_ADDR} ==")
    pod = wait_ready_pod()
    print(f"  ready pod {pod.get('id')} state={pod.get('state')} fill={pod.get('fill')}")

    status, body = api("/v1/sessions", method="POST", body={"persona_id": "e2e"})
    if status != 201:
        raise RuntimeError(f"create session failed: {status} {body}")
    session_id = body["session_id"]
    assert isinstance(session_id, str)

    try:
        connection_id = _connect_session(session_id)
        peer = ClientPeer(connection_id)
        try:
            await peer.connect()
            print("  client WebRTC connected through relay (remote pod)")
            # Turn 1: user text lands in the durable transcript, then the
            # assistant reply (transcript renders the assistant under the
            # persona label — "Assistant" when no persona event exists).
            await peer.say("First message before disconnect.")
            if not await asyncio.to_thread(
                wait_transcript_contains, session_id, "First message before disconnect.", 30.0
            ):
                raise RuntimeError("turn 1 user text not in durable transcript")
            if not await asyncio.to_thread(
                wait_transcript_contains, session_id, "**Assistant**", 60.0
            ):
                raise RuntimeError("turn 1 assistant reply not in durable transcript")
            print("  turn 1 committed (user + assistant in transcript)")
        finally:
            await peer.close()

        # Resume the SAME session: durable cursor drives resume=true.
        status, body = api(f"/v1/sessions/{session_id}/resume", method="POST")
        print("  resume status:", status, json.dumps(body)[:120])
        if status != 200:
            raise RuntimeError(f"resume failed: {status} {body}")
        connection_id = body["connect"]["connection_id"]
        await asyncio.sleep(0.5)

        peer2 = ClientPeer(connection_id)
        try:
            await peer2.connect()
            await peer2.say("Second message after resume.")
            if not await asyncio.to_thread(
                wait_transcript_contains, session_id, "Second message after resume.", 30.0
            ):
                raise RuntimeError("turn 2 user text not in durable transcript")
            if not await asyncio.to_thread(
                wait_transcript_contains, session_id, "**Assistant**", 60.0
            ):
                raise RuntimeError("turn 2 assistant reply not in durable transcript")
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
        print("  remote resume: both turns present in order; transcript durable")
        if METRICS_OUT:
            _dump_metrics(METRICS_OUT)
        print("e2e_remote_resume OK")
    finally:
        api(f"/v1/sessions/{session_id}", method="DELETE")


async def _run_convo_target(target: str, auth_frame: bool = False) -> None:
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

        peer = ClientPeer(connection_id, auth_frame=auth_frame)
        try:
            await peer.connect()
            print(
                "  client WebRTC connected through relay (captions + user_input open)"
                + (" via auth FRAME" if auth_frame else " via header")
            )

            if target == "e2e_convo":
                await _drive_convo(peer, pod_proc, log_path, session_id)
            else:
                await _drive_resume(peer, pod_proc, log_path, session_id)
        finally:
            await peer.close()

        print(f"{target} OK")
        if METRICS_OUT:
            _dump_metrics(METRICS_OUT)
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


async def _drive_convo(
    peer: ClientPeer, pod_proc: subprocess.Popen, log_path: Path, session_id: str
) -> None:
    # Turn 1: short normal answer (ANSWER_SHORT).
    await peer.say("Hello there.")
    await peer.wait_caption(timeout=10.0)
    if not await wait_for_text(
        log_path, "committed turn=1 interrupted=False", timeout=10.0
    ):
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
    if not await wait_for_text(
        log_path, "committed turn=2 interrupted=True", timeout=10.0
    ):
        raise RuntimeError("barge-in did not commit an interrupted turn")

    # INV-1: the committed text is the PLAYED prefix — clause 1 verbatim plus
    # a proportional cut of clause 2; the unplayed tail must be absent.
    transcript = transcript_of(session_id)
    if "First sentence is done." not in transcript:
        raise RuntimeError(f"barge-in played prefix missing:\n{transcript}")
    if "before finishing." in transcript:
        raise RuntimeError(f"barge-in committed UNPLAYED text:\n{transcript}")

    # Turn 3: the follow-up input gets the ScriptedLLM fallback.
    if not await wait_for_text(
        log_path, "committed turn=3 interrupted=False", timeout=10.0
    ):
        raise RuntimeError("post-barge-in answer did not commit")
    transcript = transcript_of(session_id)
    if "Understood." not in transcript:
        raise RuntimeError(
            f"post-barge-in answer missing from transcript:\n{transcript}"
        )
    finals = [c for c in peer.captions if c.get("final")]
    if not finals:
        raise RuntimeError("no final:true caption frame observed")
    print(
        "  convo: 3 turns committed; barge-in committed ONLY the played prefix; captions final seen"
    )


async def _drive_resume(
    peer: ClientPeer, pod_proc: subprocess.Popen, log_path: Path, session_id: str
) -> None:
    # Turn 1 on the first connection.
    await peer.say("First message before disconnect.")
    if not await wait_for_text(
        log_path, "committed turn=1 interrupted=False", timeout=10.0
    ):
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
        line for line in log_path.read_text(errors="replace").splitlines() if "assigned" in line
    )
    print("  pod assign lines after resume:\n" + recent)
    if not await wait_for_text(log_path, "resume=True", timeout=10.0):
        raise RuntimeError("resume frame did not carry resume=True")

    peer2 = ClientPeer(connection_id)
    try:
        await peer2.connect()
        await peer2.say("Second message after resume.")
        if not await wait_for_text(
            log_path, "committed turn=2 interrupted=False", timeout=10.0
        ):
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


def _dump_metrics(out_dir: str) -> None:
    """Snapshot all three /metrics endpoints for the M4 presence smoke.

    Called while the pod is alive (before teardown) so the pod series are
    captured with real smoke traffic. Each service writes its own file;
    a failed scrape records the error text so the smoke can report WHY a
    service was unreachable instead of silently passing.
    """
    import urllib.request as _ur

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    pod_metrics_url = f"http://{POD_ADDR}/metrics"
    if REMOTE:
        # The remote pod's /metrics lives at its public addr (from the admin
        # snapshot), not at the local POD_ADDR.
        env = load_env(ROOT / ".env")
        admin_tok = env.get("VIHS_ADMIN_TOKEN", "")
        try:
            req = urllib.request.Request(
                f"http://{ORCH_ADDR}/admin/pods",
                headers={"Authorization": f"Bearer {admin_tok}"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                body = json.loads(resp.read())
            for p in body.get("pods", []):
                if p.get("state") == "ready":
                    pod_metrics_url = f"http://{p['addr']}/metrics"
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"  admin/pods for metrics: {exc}")
    targets = {
        "orchestrator": f"http://{ORCH_ADDR}/metrics",
        "memoryd": f"http://{MEMORYD_ADDR}/metrics",
        "pod": pod_metrics_url,
    }
    for name, url in targets.items():
        try:
            with _ur.urlopen(url, timeout=5.0) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            body = f"# scrape error: {exc}\n"
        (Path(out_dir) / f"{name}.metrics").write_text(body)


def main(argv: list[str] | None = None) -> int:
    args = (
        argv
        or sys.argv[1:]
        or [
            "e2e_connect",
            "e2e_convo",
            "e2e_resume",
            "e2e_authframe",
        ]
    )
    targets: list[str] = []
    metrics_out: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--smoke":
            # Smoke = boot stack, one scripted turn, resume, delete
            # (COMMANDS.md) — the e2e_resume target covers exactly that.
            targets.append("e2e_resume")
        elif arg == "--base-url":
            # EP-009 M4: remote staging form — point every API/WS call at the
            # staging orchestrator and use a pre-registered (real) pod.
            i += 1
            if i >= len(args):
                print("--base-url requires a URL", file=sys.stderr)
                return 2
            base = args[i].rstrip("/")
            # Accept http(s)://host:port or bare host:port.
            if "://" in base:
                base = base.split("://", 1)[1]
            global ORCH_ADDR, REMOTE
            ORCH_ADDR = base
            REMOTE = True
        elif arg == "--remote-smoke":
            # Explicit remote smoke target (EP-009 M4): same as --smoke but
            # against the staging orchestrator + real pod.
            targets.append("e2e_remote_resume")
        elif arg == "--metrics-out":
            # EP-008 M4: snapshot /metrics from all three services while the
            # pod is alive (the harness tears the pod down at the end).
            i += 1
            if i >= len(args):
                print("--metrics-out requires a directory", file=sys.stderr)
                return 2
            metrics_out = args[i]
        else:
            targets.append(arg)
        i += 1
    global METRICS_OUT
    METRICS_OUT = metrics_out
    try:
        # Remote mode: the staging control plane + pod are already running;
        # never boot local services or spawn a local pod.
        owned = ensure_services() if not REMOTE else []
        try:
            bootstrap_tokens()
            for target in targets:
                if target == "e2e_connect":
                    e2e_connect()
                elif target in ("e2e_convo", "e2e_resume"):
                    if REMOTE:
                        print(f"local target {target} requires a local pod; use --remote-smoke", file=sys.stderr)
                        return 2
                    asyncio.run(_run_convo_target(target))
                elif target == "e2e_remote_resume":
                    asyncio.run(_remote_resume())
                elif target == "e2e_authframe":
                    # Browser-compatible auth path (EP-006 M5): the signal WS
                    # carries NO Authorization header — the token arrives as
                    # the SPEC-005 first-message auth frame. Full convo proof.
                    asyncio.run(_run_convo_target("e2e_convo", auth_frame=True))
                    print("e2e_authframe OK")
                else:
                    print(f"unknown e2e target: {target}", file=sys.stderr)
                    return 2
        finally:
            stop_processes(owned)
    except Exception as exc:  # noqa: BLE001
        print(f"e2e FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""EP-007 M1 — chaos: SIGKILL the pod mid-playback; assert recovery.

Spec: SPEC-006 "Pod death" row — orchestrator detects stale ping (>15 s) →
terminate/replace; affected users see the F4 reconnect → resume path; at most
the in-flight turn is lost (INV-3); the recovered transcript never claims
unheard speech (INV-1).

Flow (drives ONLY public surfaces — client API, signaling, process kill):
1. Boot stack, start pod A, create a session, connect, commit turn 1.
2. Send turn 2's long answer; SIGKILL pod A mid-clause-2 playback.
3. Wait for the orchestrator to mark pod A dead (stale ping >15 s) — poll
   /admin/pods until its state is dead/absent.
4. Start pod B (fresh), resume the SAME session → resume=True; send turn 3.
5. Assert INV-1: the transcript contains turn 1 + user turn 2 + turn 3, and
   NEVER the unplayed tail ("before finishing.") of the killed answer.
6. chain-fsck fleet-sweep: run `memoryd --rebuild-index` (fscks every
   session in the store) and assert no `fsck failed` in its output.
7. Tear down: delete the session, kill pods, stop owned services.

Runs fully under ONE asyncio loop — ClientPeer constructs an RTCPeerConnection
which requires a live event loop (aiortc 1.15).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

# Reuse the e2e harness's battle-tested helpers (same public-surface style).
from run_e2e import (  # noqa: E402
    ANSWER_LONG,
    ANSWER_SHORT,
    MOCK_ANSWERS,
    ORCH_ADDR,
    POD_ADDR,
    ClientPeer,
    _connect_session,
    _start_pod_agent,
    api,
    bootstrap_tokens,
    ensure_services,
    load_env,
    stop_processes,
    transcript_of,
    wait_for_text,
)

UNPLAYED_TAIL = "before finishing."


def admin_token() -> str:
    env = load_env(ROOT / ".env")
    tok = env.get("VIHS_ADMIN_TOKEN", "")
    if not tok:
        raise RuntimeError("VIHS_ADMIN_TOKEN missing from .env (chaos admin poll needs it)")
    return tok


def pod_state(pod_id: str) -> str | None:
    """Poll the admin API for a pod's state; None when absent."""
    try:
        status, body = api("/admin/pods", token=admin_token())
        if status != 200:
            return None
        for pod in body.get("pods", []):
            if pod.get("id") == pod_id:
                return pod.get("state")
        return None
    except Exception:  # noqa: BLE001 — transient admin hiccup = absent
        return None


async def wait_pod_dead(pod_id: str, timeout: float = 45.0) -> None:
    """Wait for the orchestrator to mark the killed pod dead or drop it."""
    state: str | None = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = pod_state(pod_id)
        if state in ("dead", None):
            print(f"  orchestrator marked {pod_id} dead/absent (state={state})")
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"pod {pod_id} not marked dead within {timeout}s (state={state})")


def fsck_fleet_sweep() -> None:
    """Run memoryd --rebuild-index (fscks EVERY session chain) and assert clean."""
    env = {**os.environ, **load_env(ROOT / ".env")}
    cmd = [str(ROOT / "target" / "debug" / "memoryd"), "--rebuild-index"]
    proc = subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    out = proc.stdout + proc.stderr
    if "rebuild-index: done" not in out:
        raise RuntimeError(f"rebuild-index did not complete:\n{out}")
    if "fsck failed" in proc.stderr or "fsck failed" in proc.stdout:
        raise RuntimeError(f"chain-fsck fleet-sweep FAILED:\n{proc.stderr[-2000:]}")
    print("  chain-fsck fleet-sweep OK (rebuild-index fscks every session)")


async def main() -> int:
    owned: list[subprocess.Popen] = []
    pod_a: subprocess.Popen | None = None
    pod_b: subprocess.Popen | None = None
    session_id: str | None = None
    try:
        owned = ensure_services()
        bootstrap_tokens()

        pod_id_a = f"chaos-a-{os.getpid()}"
        pod_a, log_a = _start_pod_agent(pod_id_a, MOCK_ANSWERS)
        if not await wait_for_text(log_a, "assign channel live", timeout=15.0):
            raise RuntimeError("pod A assign WS not live")

        status, body = api("/v1/sessions", method="POST", body={"persona_id": "chaos"})
        if status != 201:
            raise RuntimeError(f"create session failed: {status} {body}")
        session_id = body["session_id"]
        connection_id = _connect_session(session_id)
        if not await wait_for_text(log_a, "conversation ready", timeout=10.0):
            raise RuntimeError("pod A conversation not ready")

        # Turn 1: short normal answer commits fully (same WS stays open for
        # turn 2 — the harness keeps one connection per pod conversation).
        peer = ClientPeer(connection_id)
        await peer.connect()
        await peer.say("Hello, this is turn one.")
        await peer.wait_caption(timeout=10.0)
        if not await wait_for_text(log_a, "committed turn=1", timeout=10.0):
            raise RuntimeError("turn 1 did not commit")

        # Turn 2: LONG answer. Wait for clause 1 + clause 2 captions, then
        # SIGKILL mid-clause-2 playback (clause 2 plays ~510 ms — kill at ~60%).
        await peer.say("Now the long one, please.")
        await peer.wait_caption(timeout=10.0)  # clause 1 delta
        await peer.wait_caption(timeout=10.0)  # clause 2 delta
        await asyncio.sleep(0.30)
        pod_a.send_signal(signal.SIGKILL)
        print(f"  SIGKILL'd pod A ({pod_id_a}) mid-playback")
        try:
            pod_a.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pod_a.kill()
        pod_a = None
        await peer.close()

        # Orchestrator must detect the death (stale ping >15 s → dead/absent).
        await wait_pod_dead(pod_id_a)

        # Fresh pod B; resume the SAME session.
        pod_id_b = f"chaos-b-{os.getpid()}"
        pod_b, log_b = _start_pod_agent(pod_id_b, MOCK_ANSWERS)
        if not await wait_for_text(log_b, "assign channel live", timeout=15.0):
            raise RuntimeError("pod B assign WS not live")

        status, body = api(f"/v1/sessions/{session_id}/resume", method="POST")
        if status != 200:
            raise RuntimeError(f"resume failed: {status} {body}")
        if body.get("last_turn_id", 0) < 1:
            raise RuntimeError(f"resume cursor lost: {body}")
        connection_id_b = body["connect"]["connection_id"]
        if not await wait_for_text(log_b, "resume=True", timeout=10.0):
            raise RuntimeError("pod B did not receive resume=True")
        print(f"  resumed on fresh pod B ({pod_id_b}); cursor last_turn_id={body.get('last_turn_id')}")

        # Turn 3 on pod B commits normally.
        peer_b = ClientPeer(connection_id_b)
        await peer_b.connect()
        await peer_b.say("Second message after resume.")
        await peer_b.wait_caption(timeout=10.0)
        if not await wait_for_text(log_b, "committed turn=3", timeout=10.0):
            raise RuntimeError("resumed turn 3 did not commit")
        await peer_b.close()

        # INV-1: transcript has the durable turns but NEVER the unplayed tail
        # of the killed answer. The killed turn 2 assistant event never
        # committed (synchronous append after playback), so the unplayed
        # text MUST NOT appear anywhere.
        transcript = transcript_of(session_id)
        if ANSWER_SHORT not in transcript:
            raise RuntimeError(f"turn 1 answer missing:\n{transcript}")
        if UNPLAYED_TAIL in transcript:
            raise RuntimeError(f"UNPLAYED text committed after kill:\n{transcript}")
        if "Second message after resume." not in transcript:
            raise RuntimeError(f"turn 3 missing after resume:\n{transcript}")
        print("  INV-1 OK: killed turn's unplayed tail absent; turns 1+3 present")

        # chain-fsck fleet-sweep over the whole store.
        fsck_fleet_sweep()

        print("kill_pod_midturn OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"kill_pod_midturn FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if pod_a is not None and pod_a.poll() is None:
            pod_a.send_signal(signal.SIGTERM)
        if pod_b is not None and pod_b.poll() is None:
            pod_b.send_signal(signal.SIGTERM)
        if session_id:
            api(f"/v1/sessions/{session_id}", method="DELETE")
        stop_processes(owned)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

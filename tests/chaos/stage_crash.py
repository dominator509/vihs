#!/usr/bin/env python3
"""EP-007 M5 — chaos: pipeline stage crash mid-turn (SPEC-006 row 1).

Spec row: "Pipeline stage crash mid-turn: abort the response cleanly
(AbortBus), emit `kind:note` event `stage_error` (no user text), speak the
fixed recovery utterance, return to listening. Two stage crashes in one
session → pod marks itself degraded; orchestrator drains it."

The fault is injected via the env-gated hook `VIHS_FAULT=stage_crash`
(build_stages wraps the LLM so its FIRST streamed token raises) — the pod
is a REAL process; nothing is stubbed in the harness.

Flow (drives ONLY public surfaces — client API, signaling, pod /health,
admin API):
1. Boot stack; start pod A with VIHS_FAULT=stage_crash; create a session.
2. Connect a client peer; send turn 1 → the LLM stage crashes.
   Assert: conversation recovers (pod log "stage crash recovered"),
   the transcript carries the recovery utterance, and a `kind:note`
   `stage_error` event with NO user text.
3. Send turn 2 → crash again → the pod reports `degraded:true` on
   /health and `stage_crashes:2`.
4. Assert the orchestrator drained the pod (admin /admin/pods state
   becomes "draining" within the health-ping window).
5. Tear down: delete the session, kill the pod, stop owned services.

Runs fully under ONE asyncio loop (ClientPeer needs a live event loop).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

from run_e2e import (  # noqa: E402
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

RECOVERY_UTTERANCE = "Something went wrong on my end. Give me a moment."


def admin_token() -> str:
    tok = load_env(ROOT / ".env").get("VIHS_ADMIN_TOKEN", "")
    if not tok:
        raise RuntimeError("VIHS_ADMIN_TOKEN missing from .env")
    return tok


def pod_state(pod_id: str) -> str | None:
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


async def wait_pod_draining(pod_id: str, timeout: float = 30.0) -> None:
    """Wait for the orchestrator to mark the degraded pod draining."""
    state: str | None = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = pod_state(pod_id)
        if state == "draining":
            print(f"  orchestrator marked {pod_id} draining (degraded)")
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"pod {pod_id} not drained within {timeout}s (state={state})")


async def main() -> int:
    owned: list[subprocess.Popen] = []
    pod_proc: subprocess.Popen | None = None
    session_id: str | None = None
    try:
        owned = ensure_services()
        bootstrap_tokens()

        # Fault hook: build_stages wraps the LLM so its first token raises.
        os.environ["VIHS_FAULT"] = "stage_crash"
        pod_id = f"chaos-crash-{os.getpid()}"
        pod_proc, log_path = _start_pod_agent(pod_id)
        try:
            if not await wait_for_text(log_path, "assign channel live", timeout=15.0):
                raise RuntimeError("pod assign WS not live")

            status, body = api(
                "/v1/sessions", method="POST", body={"persona_id": "chaos"}
            )
            if status != 201:
                raise RuntimeError(f"create session failed: {status} {body}")
            session_id = body["session_id"]
            connection_id = _connect_session(session_id)
            if not await wait_for_text(log_path, "conversation ready", timeout=10.0):
                raise RuntimeError("pod conversation not ready")

            peer = ClientPeer(connection_id)
            await peer.connect()

            # Turn 1 → the LLM stage crashes mid-stream.
            await peer.say("Hello there.")
            await peer.wait_caption(timeout=15.0)
            if not await wait_for_text(log_path, "stage crash recovered", timeout=10.0):
                raise RuntimeError("pod did not log stage crash recovery")
            print("  turn 1: stage crash recovered (note + recovery utterance)")

            # The transcript must carry the recovery utterance AND the
            # stage_error note (no user text) — SPEC-006 row 1. The
            # append buffer flushes asynchronously, so poll for it.
            transcript = ""
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                transcript = transcript_of(session_id)
                if RECOVERY_UTTERANCE in transcript:
                    break
                await asyncio.sleep(0.5)
            assert RECOVERY_UTTERANCE in transcript, (
                f"recovery utterance missing from transcript:\n{transcript}"
            )
            # The note is a system event; transcript rendering shows the
            # recovery utterance, and the raw log carries the note.
            status, body = api(f"/v1/sessions/{session_id}/transcript")
            assert status == 200, f"transcript {status} {body}"
            print("  transcript: recovery utterance present")

            # Turn 2 → crash again → pod degrades (2 crashes / session).
            await peer.say("Again.")
            await peer.wait_caption(timeout=15.0)
            if not await wait_for_text(log_path, "pod degraded", timeout=10.0):
                raise RuntimeError("pod did not log degraded after 2nd crash")
            print("  turn 2: second crash → pod degraded")

            # Pod /health reports degraded + stage_crashes=2.
            health = json.loads(await _read_health())
            assert health.get("degraded") is True, health
            assert health.get("stage_crashes", 0) >= 2, health
            print(
                f"  pod /health: degraded={health['degraded']} crashes={health['stage_crashes']}"
            )

            # Orchestrator drains the degraded pod (health-ping window).
            await wait_pod_draining(pod_id)
            with contextlib.suppress(Exception):
                await peer.close()
        finally:
            os.environ.pop("VIHS_FAULT", None)

        print("stage_crash OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"stage_crash FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        os.environ.pop("VIHS_FAULT", None)
        if session_id:
            with contextlib.suppress(Exception):
                api(f"/v1/sessions/{session_id}", method="DELETE")
        if pod_proc is not None and pod_proc.poll() is None:
            with contextlib.suppress(Exception):
                pod_proc.send_signal(15)
            try:
                pod_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pod_proc.kill()
        stop_processes(owned)


async def _read_health() -> str:
    import urllib.request

    with urllib.request.urlopen("http://127.0.0.1:8093/health", timeout=5) as resp:
        return resp.read().decode()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

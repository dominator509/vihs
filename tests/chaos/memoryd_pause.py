#!/usr/bin/env python3
"""EP-007 M2 — chaos: SIGSTOP memoryd during turns; assert R2 buffer recovers.

Spec: SPEC-006 "memoryd down" row + ARCHITECTURE §9 — pods buffer committed
events in a bounded queue (max 64) with retry/backoff; the media path never
blocks on memoryd; on drain the buffer empties; committed turns are never
lost.

Flow (public surfaces only):
1. Boot stack, start a pod, create a session, connect, commit turn 1.
2. SIGSTOP memoryd (freeze, not kill) for ~30 s while turns keep flowing.
3. Assert: pod /health shows append_buffer_depth rising (the R2 buffer is
   absorbing events), the media path NEVER stalls (captions/turns still
   commit during the pause — the mock pipeline is in-process), and no
   events are dropped (buffer depth stays well under max 64).
4. SIGCONT memoryd.
5. Assert: buffer drains to 0 and the transcript contains EVERY committed
   turn (no lost committed turns).
6. Tear down: delete session, kill pod, stop owned services.

Runs fully under ONE asyncio loop (ClientPeer needs a live event loop).
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

from run_e2e import (  # noqa: E402
    MOCK_ANSWERS,
    POD_ADDR,
    ClientPeer,
    _connect_session,
    _start_pod_agent,
    api,
    bootstrap_tokens,
    ensure_services,
    stop_processes,
    transcript_of,
    wait_for_text,
)

PAUSE_S = 30
TURNS_DURING_PAUSE = 3  # enough to prove media keeps flowing while frozen


def memoryd_pid() -> int:
    out = subprocess.run(
        ["pgrep", "-af", "memoryd"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    # pgrep -af returns lines "PID CMD". The actual memoryd binary is the
    # line whose COMMAND starts with the binary path (not a bash wrapper).
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid, cmd = parts
        if (
            cmd.startswith("/root/vihs/target/debug/memoryd")
            or cmd.startswith("/root/vihs/target/release/memoryd")
            or cmd.startswith("./target/debug/memoryd")
            or cmd.startswith("./target/release/memoryd")
            or cmd.startswith("target/debug/memoryd")
            or cmd.startswith("target/release/memoryd")
        ):
            return int(pid)
    raise RuntimeError("memoryd binary process not found (pgrep output:\n" + out + ")")


def buffer_depth() -> int:
    """Read the pod /health append_buffer_depth (0 when no conversation)."""
    try:
        status, body = http_get_json(f"http://{POD_ADDR}/health")
        if status != 200:
            return -1
        return int(body.get("append_buffer_depth", -1))
    except Exception:  # noqa: BLE001 — pod may be mid-restart
        return -1


def http_get_json(url: str) -> tuple[int, dict]:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except Exception as exc:  # noqa: BLE001
        return 0, {"_error": str(exc)}


async def main() -> int:
    owned: list[subprocess.Popen] = []
    pod_proc: subprocess.Popen | None = None
    session_id: str | None = None
    try:
        owned = ensure_services()
        bootstrap_tokens()

        pod_id = f"chaos-pause-{os.getpid()}"
        pod_proc, log_path = _start_pod_agent(pod_id, MOCK_ANSWERS)
        if not await wait_for_text(log_path, "assign channel live", timeout=15.0):
            raise RuntimeError("pod assign WS not live")

        status, body = api("/v1/sessions", method="POST", body={"persona_id": "chaos"})
        if status != 201:
            raise RuntimeError(f"create session failed: {status} {body}")
        session_id = body["session_id"]
        connection_id = _connect_session(session_id)
        if not await wait_for_text(log_path, "conversation ready", timeout=10.0):
            raise RuntimeError("pod conversation not ready")

        peer = ClientPeer(connection_id)
        await peer.connect()
        await peer.say("Hello, turn one before the freeze.")
        await peer.wait_caption(timeout=10.0)
        if not await wait_for_text(log_path, "committed turn=1", timeout=10.0):
            raise RuntimeError("turn 1 did not commit")

        # --- SIGSTOP memoryd ---
        mem_pid = memoryd_pid()
        os.kill(mem_pid, signal.SIGSTOP)
        print(f"  SIGSTOP memoryd (pid {mem_pid}) for {PAUSE_S}s")
        try:
            # Send pause turns ONE at a time and wait for each commit. Each
            # turn's prompt assembly reads memoryd (5 s timeout while frozen),
            # so a commit lands every ~5-7 s. Waiting for the commit proves
            # the media path itself never blocks on memoryd — only prompt
            # assembly degrades, and that is bounded by the client timeout.
            depth_samples: list[int] = []
            pause_turn = 0
            deadline = time.monotonic() + PAUSE_S
            while time.monotonic() < deadline:
                if pause_turn < TURNS_DURING_PAUSE:
                    pause_turn += 1
                    await peer.say(f"Paused turn {pause_turn}.")
                    if not await wait_for_text(
                        log_path, f"committed turn={pause_turn + 1}", timeout=8.0
                    ):
                        raise RuntimeError(
                            f"pause turn {pause_turn} did not commit (media stalled)"
                        )
                await asyncio.sleep(2.0)
                depth_samples.append(buffer_depth())
            print(
                f"  during pause: {pause_turn} turns committed while frozen; "
                f"buffer depth samples: {depth_samples}"
            )
            if pause_turn < 1:
                raise RuntimeError("no pause turn committed — media stalled")
            # The R2 buffer must have absorbed events (depth rose). While
            # memoryd is frozen, the flusher holds one event in retry, so
            # depth must be ≥1 for at least one sample.
            if max(depth_samples, default=0) <= 0:
                raise RuntimeError(
                    "append_buffer_depth never rose while memoryd frozen"
                )
            # Must NOT have overflowed (max 64) — no drops, no degrade.
            if max(depth_samples) >= 64:
                raise RuntimeError(f"buffer overflowed (depth={max(depth_samples)})")
            print(
                "  media never stalled: all pause turns committed while memoryd frozen"
            )
        finally:
            os.kill(mem_pid, signal.SIGCONT)
            print("  SIGCONT memoryd")

        # --- drain ---
        drained = False
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if buffer_depth() == 0:
                drained = True
                break
            await asyncio.sleep(1.0)
        if not drained:
            raise RuntimeError(f"buffer did not drain to 0 (depth={buffer_depth()})")
        # Give the flusher a moment to land the last events in memoryd, then
        # verify NO committed turn was lost.
        await asyncio.sleep(1.0)
        transcript = transcript_of(session_id)
        # Turn 1 = pre-freeze; pause turns i commit as turn i+1 with text
        # "Paused turn {i}.".
        if "Hello, turn one before the freeze." not in transcript:
            raise RuntimeError(f"turn 1 LOST after drain:\n{transcript}")
        for i in range(1, pause_turn + 1):
            needle = f"Paused turn {i}."
            if needle not in transcript:
                raise RuntimeError(f"pause turn {i} LOST after drain:\n{transcript}")
        print("  buffer drained to 0; ALL committed turns present (none lost)")

        print("memoryd_pause OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"memoryd_pause FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if pod_proc is not None and pod_proc.poll() is None:
            pod_proc.send_signal(signal.SIGTERM)
        if session_id:
            api(f"/v1/sessions/{session_id}", method="DELETE")
        stop_processes(owned)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

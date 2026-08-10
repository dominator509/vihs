#!/usr/bin/env python3
"""EP-007 M3 — chaos: Redis loss → readyz fails → --rebuild-index restores.

Spec: SPEC-006 "Redis down" row — "memoryd/orchestrator fail readyz; on
recovery or replacement, `--rebuild-index` path documented (OPERATIONS)".
Redis is disposable (ADR-003); the object store is the source of truth.

Flow (public surfaces + documented infrastructure fault injection):
1. Boot stack, mint a user token, create a session, commit real events
   (system note + user + assistant → durable chain in MinIO).
2. Snapshot the transcript BEFORE the loss.
3. FLUSHALL Redis (docker exec redis-cli — wipes session hashes, owner
   zsets, AND the token store: every credential dies with Redis).
4. Assert readyz FAILS (503) on both memoryd and orchestrator once the
   token store is gone — actually the store is unreachable-checked, so:
   memoryd readyz = Redis PING (still up after FLUSHALL, so 200); the
   honest degradation is that every API call now 401s (token gone) and
   the session list is empty.
   The SPEC row is about Redis DOWN — verify that with a real stop/start
   of the container: readyz 503 while Redis is down, 200 after recovery.
   Then restart memoryd (GAP-M3-5: its pooled Redis connection does not
   survive a container bounce; SPEC-006 recovery = 'on recovery or
   replacement', i.e. the service is restarted).
5. Run `memoryd --rebuild-index` (documented OPERATIONS recovery) →
   session index + owner binding restored from the object store.
6. Restart the orchestrator (tokens were wiped — re-seeded from .env at
   startup) → the in-memory session index is warmed from Redis (EP-007 M3
   GAP-M3-4), owner binding restored (GAP-M3-3).
7. Mint a fresh user token; assert transcript EQUALS the pre-loss snapshot
   and the session appears in GET /v1/sessions.
8. Tear down: delete the session, stop owned services.
"""

from __future__ import annotations

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
    MEMORYD_ADDR,
    api,
    bootstrap_tokens,
    ensure_services,
    load_env,
    stop_processes,
    transcript_of,
)

REDIS_CONTAINER = "docker-redis-1"


def readyz(port: int) -> tuple[int, str]:
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/readyz", timeout=5
        ) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def docker(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *cmd], capture_output=True, text=True, timeout=timeout
    )


def redis_cli(*args: str) -> str:
    """Run redis-cli inside the dev redis container."""
    proc = docker(["exec", REDIS_CONTAINER, "redis-cli", *args])
    if proc.returncode != 0:
        raise RuntimeError(
            f"redis-cli {' '.join(args)} failed: {proc.stdout} {proc.stderr}"
        )
    return proc.stdout


def orchestrator_pid() -> int | None:
    """Find the orchestrator's real PID by its listener (not pgrep — the
    M2 SIGSTOP trap: pgrep -f matches the bash wrapper AND the binary)."""
    return listener_pid(8080, "orchestrator")


def listener_pid(port: int, name: str) -> int | None:
    proc = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=10)
    for line in proc.stdout.splitlines():
        if f":{port} " in line and name in line:
            import re

            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def wait_readyz(port: int, expect_ok: bool, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _ = readyz(port)
        if (status == 200) == expect_ok:
            return
        time.sleep(0.5)
    raise RuntimeError(f"readyz on :{port} did not reach ok={expect_ok}")


def restart_orchestrator() -> tuple[str, subprocess.Popen]:
    """Kill the orchestrator (token store wiped by FLUSHALL) and start a
    fresh one — .env tokens are re-seeded at startup and the session index
    is warmed from Redis (GAP-M3-4). Returns (name, Popen) we may own."""
    pid = orchestrator_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
    env = {**os.environ, **load_env(ROOT / ".env")}
    # Hermetic bind: .env may carry a staging/public VIHS_ORCH_ADDR (EP-009
    # RunPod deploys) that would bind only the public IP — the drill's
    # wait_readyz(8080) polls loopback and would time out. Force 0.0.0.0 so
    # both 127.0.0.1 and the public addr answer.
    env["VIHS_ORCH_ADDR"] = "0.0.0.0:8080"
    proc = subprocess.Popen(
        [str(ROOT / "target" / "debug" / "orchestrator")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_readyz(8080, True)
    return ("orchestrator", proc)


def restart_memoryd() -> tuple[str, subprocess.Popen]:
    """Kill memoryd and start a fresh one. GAP-M3-5: memoryd's long-lived
    Redis connection is built once at startup and does NOT survive a Redis
    container restart (the token-store verify path goes stale → 401/503 on
    every request even though Redis is back). SPEC-006's documented recovery
    is 'on recovery or replacement' — the service is restarted. Mirror the
    orchestrator restart so the post-loss assertions run against a healthy
    connection, not the stale one."""
    pid = listener_pid(8091, "memoryd")
    if pid:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
    env = {**os.environ, **load_env(ROOT / ".env")}
    # Hermetic bind (see restart_orchestrator): 0.0.0.0 keeps loopback
    # checks AND the staging public addr working after the drill.
    env["VIHS_MEMORYD_ADDR"] = "0.0.0.0:8091"
    proc = subprocess.Popen(
        [str(ROOT / "target" / "debug" / "memoryd")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_readyz(8091, True)
    return ("memoryd", proc)


def main() -> int:
    owned: list[subprocess.Popen] = []
    restarted: dict[str, subprocess.Popen] = {}
    session_id: str | None = None
    pre_running: dict[str, bool] = {
        "memoryd": False,
        "orchestrator": False,
    }
    try:
        # Pre-drill state: the chaos gate runs INSIDE verify.sh between
        # test-e2e and build/smoke, so memoryd+orchestrator are usually
        # ALREADY up. The drill restarts them (fresh connections); at
        # teardown it must leave the stack exactly as healthy as it found
        # it — kill only what it started itself, keep what was pre-existing.
        pre_running = {
            "memoryd": listener_pid(8091, "memoryd") is not None,
            "orchestrator": listener_pid(8080, "orchestrator") is not None,
        }
        owned = ensure_services()
        # Known-good baseline: this drill injects faults, so it must start
        # from fresh processes. ensure_services only checks healthz (which
        # is unconditional "ok"), so a stale Redis pipe left over from an
        # earlier drill would fail the pre-loss assertions for the wrong
        # reason (GAP-M3-5 root cause). Restart both services now: fresh
        # Redis connections + .env tokens re-seeded into the store.
        restarted["memoryd"] = restart_memoryd()[1]
        restarted["orchestrator"] = restart_orchestrator()[1]
        bootstrap_tokens()

        from run_e2e import USER_TOKEN  # noqa: PLC0415 — after bootstrap

        # 1. Create a session and commit a real chain.
        status, body = api("/v1/sessions", method="POST", body={"persona_id": "rl"})
        if status != 201:
            raise RuntimeError(f"create session failed: {status} {body}")
        session_id = body["session_id"]

        # memoryd /events with the user token (create-path owner binding).
        import urllib.request

        now = "2026-08-08T00:00:00Z"
        events = [
            {
                "v": 1,
                "session_id": session_id,
                "turn_id": 1,
                "ts": now,
                "role": "user",
                "kind": "utterance",
                "text": "durable me",
                "meta": {"asr_conf_bp": 9000, "interrupted": False},
            },
            {
                "v": 1,
                "session_id": session_id,
                "turn_id": 1,
                "ts": now,
                "role": "assistant",
                "kind": "utterance",
                "text": "durable reply",
                "meta": {"asr_conf_bp": 9000, "interrupted": False},
            },
        ]
        for ev in events:
            data = json.dumps(ev).encode()
            req = urllib.request.Request(
                f"http://{MEMORYD_ADDR}/v1/sessions/{session_id}/events",
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {USER_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"append status {resp.status}")
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"append failed: {e.code} {e.read()!r}") from e

        # 2. Snapshot the transcript BEFORE the loss.
        before = transcript_of(session_id)
        if "durable reply" not in before:
            raise RuntimeError(f"pre-loss transcript missing content:\n{before}")
        print("  committed durable chain; transcript snapshot taken")

        # 3. Redis DOWN → readyz fails on both services (SPEC-006 row).
        docker(["stop", REDIS_CONTAINER])
        try:
            wait_readyz(8091, False)
            wait_readyz(8080, False)
            print("  Redis down → memoryd + orchestrator readyz 503")
        finally:
            docker(["start", REDIS_CONTAINER])
        wait_readyz(8091, True)
        wait_readyz(8080, True)
        print("  Redis recovered → readyz 200")

        # 3b. GAP-M3-5 recovery step: memoryd's pooled Redis connection was
        # built before the container bounce and does not survive it. SPEC-006
        # documents 'on recovery or replacement' — restart the service so the
        # post-loss assertions run against a healthy connection (this is the
        # honest version of the drill; without it every verify 401s on a
        # stale pipe, which is the exact defect GAP-M3-5 classifies).
        restarted["memoryd"] = restart_memoryd()[1]
        print("  memoryd restarted (fresh Redis connection, GAP-M3-5 recovery)")

        # 4. FLUSHALL — the loss: index hashes, owner zsets, AND every token
        #    (token store lives in Redis) are gone.
        redis_cli("FLUSHALL")
        print("  FLUSHALL — Redis data (index + tokens) wiped")

        # Degradation: the OLD user token is dead (401) and the session is
        # gone from the orchestrator's warm view until recovery.
        status, _ = api("/v1/sessions")
        if status != 401:
            raise RuntimeError(f"expected 401 with wiped token store, got {status}")

        # 5. Rebuild the index from the object store (documented recovery).
        env = {**os.environ, **load_env(ROOT / ".env")}
        proc = subprocess.run(
            [str(ROOT / "target" / "debug" / "memoryd"), "--rebuild-index"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = proc.stdout + proc.stderr
        if "rebuild-index: done" not in out:
            raise RuntimeError(f"rebuild-index did not complete:\n{out}")
        print("  rebuild-index: index + owner binding restored from object store")

        # 6. Restart the orchestrator: tokens re-seeded from .env; session
        #    index warmed from Redis (GAP-M3-4).
        restarted["orchestrator"] = restart_orchestrator()[1]
        print("  orchestrator restarted: tokens re-seeded, session index warmed")

        # 7. Fresh user token; the session must be visible and its
        #    transcript IDENTICAL to the pre-loss snapshot.
        bootstrap_tokens()

        status, body = api("/v1/sessions")
        if status != 200:
            raise RuntimeError(f"session list after recovery: {status} {body}")
        ids = [s.get("session_id") for s in body.get("sessions", [])]
        if session_id not in ids:
            raise RuntimeError(
                f"session missing after recovery (warm-up failed): {ids}"
            )
        after = transcript_of(session_id)
        if after != before:
            raise RuntimeError(
                f"transcript changed across Redis loss:\nBEFORE:\n{before}\nAFTER:\n{after}"
            )
        print("  recovery OK: session visible, transcript identical")

        print("redis_loss_rebuild OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"redis_loss_rebuild FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if session_id:
            api(f"/v1/sessions/{session_id}", method="DELETE")
        stop_processes(owned)
        # Leave the stack as healthy as we found it (verify.sh runs chaos
        # BEFORE build/smoke — services must still be up for what follows).
        # Kill only the services the drill restarted that were NOT already
        # running when the drill began.
        for name, proc in restarted.items():
            if not pre_running.get(name, False):
                stop_processes([proc])


if __name__ == "__main__":
    sys.exit(main())

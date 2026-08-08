#!/usr/bin/env python3
"""EP-007 M3 — chaos: torn write in the object store → fsck catches it.

Spec: SPEC-006 "Chain verification failure on load" row — `integrity_hold`
(409), resume denied, SEV1, NEVER auto-"repair". SPEC-006 Required tests:
"corrupt a log copy for the fsck path".

Flow (public surfaces + documented store fault injection):
1. Boot stack, mint a user token, create a session (system note = real
   chain), append user+assistant events through memoryd's public API.
2. Fetch the session's log object (anonymous download on the dev bucket),
   corrupt a COPY: keep the JSON valid but change the LAST event's text so
   the chain hash no longer recomputes (BadHash — a torn write).
3. Write the corrupted copy back over the session's log (mc inside the dev
   MinIO container — the sanctioned store fault injection).
4. POST resume (public surface) → expect 409 integrity_hold.
5. POST memoryd /load with the user token → expect 409 integrity_hold.
6. Run `memoryd --rebuild-index` → it logs `fsck failed` for the session
   but exits 0 — rebuild never auto-repairs a broken chain (SEV1).
7. Resume STILL 409s after rebuild (never auto-repaired).
8. Tear down: hard-delete the session (delete does not fsck), stop owned
   services. The store is left clean.

NOTE on the "corrupt a COPY" language: we read the pristine object, corrupt
the copy in memory, and write THAT back as the log — the fixture session is
created and destroyed entirely by this test (never a real user session).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

from run_e2e import (  # noqa: E402
    MOCK_ANSWERS,
    MEMORYD_ADDR,
    _start_pod_agent,
    api,
    bootstrap_tokens,
    ensure_services,
    load_env,
    stop_processes,
    wait_for_text,
)

MINIO_BUCKET = "vihs-sessions"
MINIO_CONTAINER = "docker-minio-1"


def _mc(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run mc inside the dev MinIO container (store fault-injection seam)."""
    full = ["docker", "exec", MINIO_CONTAINER, "mc", *cmd]
    proc = subprocess.run(full, capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise RuntimeError(f"mc {' '.join(cmd)} failed: {proc.stdout} {proc.stderr}")
    return proc


def read_log_object(sid: str, seq: int = 1) -> str:
    """Anonymous GET of one event object (dev bucket is anonymous-download)."""
    import urllib.request

    url = f"http://127.0.0.1:9000/{MINIO_BUCKET}/sessions/{sid}/events/{seq:012}.jsonl"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read().decode()


def corrupt_copy(log_text: str) -> str:
    """Corrupt a COPY: change the LAST event's text (JSON stays valid, the
    chain hash no longer recomputes → fsck BadHash). Returns the new log."""
    lines = [ln for ln in log_text.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("log object empty — nothing to corrupt")
    last = json.loads(lines[-1])
    last["text"] = last.get("text", "") + " TORN-WRITE"
    lines[-1] = json.dumps(last, separators=(",", ":"))
    return "\n".join(lines) + "\n"


def write_log_object(sid: str, seq: int, content: str) -> None:
    """Write the corrupted copy back via mc (container-scoped temp file)."""
    tmp = f"/tmp/torn-{sid}.jsonl"
    host_tmp = Path("/tmp") / f"torn-{sid}.jsonl"
    host_tmp.write_text(content)
    subprocess.run(
        ["docker", "cp", str(host_tmp), f"{MINIO_CONTAINER}:{tmp}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    _mc(["alias", "set", "local", "http://127.0.0.1:9000", "minioadmin", "minioadmin"])
    obj = f"local/{MINIO_BUCKET}/sessions/{sid}/events/{seq:012}.jsonl"
    _mc(["cp", tmp, obj])


def memoryd_load(sid: str, token: str) -> tuple[int, str]:
    """Direct memoryd POST /load — the durable integrity gate."""
    import urllib.request

    req = urllib.request.Request(
        f"http://{MEMORYD_ADDR}/v1/sessions/{sid}/load",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def memoryd_append(sid: str, token: str, ev: dict) -> tuple[int, dict | str]:
    """Direct memoryd POST /events (SPEC-003 memoryd table — the pod path;
    the orchestrator does not proxy appends)."""
    import urllib.request

    data = json.dumps(ev).encode()
    req = urllib.request.Request(
        f"http://{MEMORYD_ADDR}/v1/sessions/{sid}/events",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, raw.decode(errors="replace")


def rebuild_index() -> str:
    """Run memoryd --rebuild-index; returns combined output."""
    env = {**os.environ, **load_env(ROOT / ".env")}
    proc = subprocess.run(
        [str(ROOT / "target" / "debug" / "memoryd"), "--rebuild-index"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.stdout + proc.stderr


async def main() -> int:
    owned: list[subprocess.Popen] = []
    pod: subprocess.Popen | None = None
    session_id: str | None = None
    try:
        owned = ensure_services()
        bootstrap_tokens()

        # The assign flow picks a READY pod before it calls memoryd load, so
        # a live mock pod is required to reach the integrity gate at all.
        pod_id = f"torn-{os.getpid()}"
        pod, pod_log = _start_pod_agent(pod_id, MOCK_ANSWERS)
        if not await wait_for_text(pod_log, "assign channel live", timeout=15.0):
            raise RuntimeError("pod assign WS not live")

        from run_e2e import USER_TOKEN  # noqa: PLC0415 — after bootstrap

        # 1. Create a session via the public API (registers owner + zset).
        status, body = api("/v1/sessions", method="POST", body={"persona_id": "torn"})
        if status != 201:
            raise RuntimeError(f"create session failed: {status} {body}")
        session_id = body["session_id"]

        # Append a real chain through memoryd's public API (user token; the
        # create-path owner binding from the orchestrator note allows it).
        now = "2026-08-08T00:00:00Z"
        events = [
            {
                "v": 1,
                "session_id": session_id,
                "turn_id": 1,
                "ts": now,
                "role": "user",
                "kind": "utterance",
                "text": "hello torn",
                "meta": {"asr_conf_bp": 9000, "interrupted": False},
            },
            {
                "v": 1,
                "session_id": session_id,
                "turn_id": 1,
                "ts": now,
                "role": "assistant",
                "kind": "utterance",
                "text": "reply one",
                "meta": {"asr_conf_bp": 9000, "interrupted": False},
            },
        ]
        for ev in events:
            status, body = memoryd_append(session_id, USER_TOKEN, ev)
            if status != 200:
                raise RuntimeError(f"append failed: {status} {body}")

        # Sanity: the pristine chain loads fine BEFORE the fault.
        status, _ = memoryd_load(session_id, USER_TOKEN)
        if status != 200:
            raise RuntimeError(f"pristine load expected 200, got {status}")

        # 2-3. Corrupt a COPY of the log and write it back (torn write).
        log_text = read_log_object(session_id, seq=1)
        torn = corrupt_copy(log_text)
        write_log_object(session_id, 1, torn)
        print("  wrote corrupted log copy back over the session's log")

        # 4. Public resume → 409 integrity_hold (SPEC-003 resume row).
        status, body = api(f"/v1/sessions/{session_id}/resume", method="POST")
        if status != 409 or body.get("error", {}).get("code") != "integrity_hold":
            raise RuntimeError(
                f"resume on torn chain expected 409 integrity_hold, got {status} {body}"
            )
        print("  resume → 409 integrity_hold (public surface)")

        # 5. memoryd load → 409 integrity_hold (durable gate).
        status, text = memoryd_load(session_id, USER_TOKEN)
        if status != 409 or "integrity_hold" not in text:
            raise RuntimeError(
                f"memoryd load on torn chain expected 409 integrity_hold, got {status} {text}"
            )
        print("  memoryd /load → 409 integrity_hold")

        # 6. rebuild-index: fscks every session, logs fsck failed, exits 0 —
        #    it must NOT auto-repair a broken chain (SEV1).
        out = rebuild_index()
        if "rebuild-index: done" not in out:
            raise RuntimeError(f"rebuild-index did not complete:\n{out}")
        if "fsck failed" not in out:
            raise RuntimeError(
                f"rebuild-index did not report the torn chain (fsck failed missing):\n{out}"
            )
        print("  rebuild-index fscks the torn chain and logs fsck failed")

        # 7. STILL integrity_hold after rebuild — never auto-repaired.
        status, body = api(f"/v1/sessions/{session_id}/resume", method="POST")
        if status != 409 or body.get("error", {}).get("code") != "integrity_hold":
            raise RuntimeError(
                f"resume after rebuild expected 409 integrity_hold (no auto-repair), "
                f"got {status} {body}"
            )
        print("  resume still 409 integrity_hold after rebuild (never auto-repaired)")

        print("torn_write_fsck OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"torn_write_fsck FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if pod is not None and pod.poll() is None:
            pod.send_signal(signal.SIGTERM)
        if session_id:
            api(f"/v1/sessions/{session_id}", method="DELETE")
        stop_processes(owned)


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))

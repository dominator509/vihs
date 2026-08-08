#!/usr/bin/env python3
"""VIHS E2E harness (EP-005 M4/M5; SPEC-001/SPEC-004 acceptance).

Targets:
  e2e_connect  — pod register → health → assign WS → WebRTC loopback +
                 captions round-trip against the live stack (M4).
  (default = e2e_connect; M5 adds the full scripted conversation targets.)

The harness creates and deletes its own session (EP-005 §11 idempotence).
Services it starts itself are killed on exit; services already running are
left alone.
"""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[2]
POD_VENV = ROOT / "pod" / ".venv" / "bin" / "python"
ORCH_ADDR = "127.0.0.1:8080"
MEMORYD_ADDR = "127.0.0.1:8091"
POD_ADDR = "127.0.0.1:8093"
USER_TOKEN = "dev-e2e-user-token"
POD_TOKEN = "dev-pod-token"
WAIT = 20.0


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


def wait_for_text(path: Path, pattern: str, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(errors="replace")
            if pattern in text:
                return True
        time.sleep(0.25)
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


def api(path: str, method: str = "GET", body: dict | None = None, token: str = USER_TOKEN) -> tuple[int, dict]:
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
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {}


def e2e_connect() -> None:
    print("== e2e_connect: pod register/health/assign + WebRTC loopback + captions ==")
    owned = ensure_services()
    pod_proc: subprocess.Popen | None = None
    session_id: str | None = None
    try:
        # --- start the pod agent ---
        pod_id = f"e2e-pod-{os.getpid()}"
        log_path = Path(tempfile.gettempdir()) / f"vihs-pod-{os.getpid()}.log"
        pod_env = {
            **os.environ,
            **load_env(ROOT / ".env"),
            "VIHS_POD_ADDR": POD_ADDR,
            "VIHS_POD_TOKEN": POD_TOKEN,
            "VIHS_POD_LOG": "info",
        }
        with open(log_path, "w") as logf:
            pod_proc = subprocess.Popen(
                [
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
                ],
                cwd=ROOT / "pod",
                env=pod_env,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )

        if not wait_for_text(log_path, "assign channel live", timeout=15.0):
            raise RuntimeError(
                "pod did not connect its assign WS (register/ack failed)\n"
                + (log_path.read_text(errors="replace") if log_path.exists() else "")
            )
        print("  pod registered + assign channel live")

        # --- create + connect a session (drives the assign frame) ---
        status, body = api("/v1/sessions", method="POST", body={"persona_id": "e2e"})
        if status != 201:
            raise RuntimeError(f"create session failed: {status} {body}")
        session_id = body["session_id"]

        status, body = api(f"/v1/sessions/{session_id}/connect", method="POST")
        if status != 200:
            raise RuntimeError(f"connect session failed: {status} {body}")
        print(f"  session {session_id[:8]}… connected: {body.get('connect', {}).get('connection_id', '?')[:8]}…")

        # --- pod must receive the assignment and prove WebRTC + captions ---
        if not wait_for_text(log_path, f"pod assigned session={session_id}", timeout=10.0):
            raise RuntimeError("pod did not receive the assign frame")
        if not wait_for_text(log_path, "captions loopback ok", timeout=15.0):
            raise RuntimeError("pod WebRTC loopback / captions round-trip did not complete")
        print("  assign frame received; WebRTC loopback + captions round-trip OK")

        # --- pod local health surface ---
        status = http_get(f"http://{POD_ADDR}/health")
        if status != 200:
            raise RuntimeError(f"pod /health returned {status}")
        print("  pod /health OK")
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


def main(argv: list[str] | None = None) -> int:
    target = (argv or sys.argv[1:] or ["e2e_connect"])[0]
    try:
        if target == "e2e_connect":
            e2e_connect()
        else:
            print(f"unknown e2e target: {target}", file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"e2e FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

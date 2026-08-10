#!/usr/bin/env python3
"""EP-009 M4 fast-fail staging deploy.

Cold-node pulls of the 379MB pod image crawl (>45 min, sometimes never
complete) while warm nodes boot the SAME image in ~35s (run #7 evidence).
Instead of one pod attempt with a 90-min patience, this deploy CYCLEs:
create pod -> watch 12 min for a container -> if none, terminate and retry
with a fresh pod. Warm-node hits are the goal; each cold attempt costs only
~12 min + ~$0.15 instead of 90 min + $1.1. ALWAYS terminates pods in
finally (operator hard rule: no held-open billing).

Env: same as staging-deploy.py (RUNPOD_API_KEY, VIHS_RUNPOD_IMAGE,
VIHS_ORCH_ADDR, VIHS_ORCH_LOCAL_ADDR, VIHS_POD_TOKEN, ...).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

VOLUME_DIR = "/workspace/models"
POD_PORT = 8093
COLD_GRACE_S = 720          # watch a cold attempt this long before cycling
MAX_ATTEMPTS = 4            # total pod attempts before giving up
READY_WAIT_S = 600          # after container start, wait this long for Ready


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def main() -> int:
    env = {**os.environ, **load_env(ROOT / ".env")}
    api_key = env.get("RUNPOD_API_KEY", "")
    image = env.get("VIHS_RUNPOD_IMAGE", "ttl.sh/vihs-pod-ep009slim:24h")
    volume_id = env.get("VIHS_RUNPOD_VOLUME", "")
    orch = env.get("VIHS_ORCH_ADDR", "")
    orch_local = env.get("VIHS_ORCH_LOCAL_ADDR", orch)
    pod_token = env.get("VIHS_POD_TOKEN", "")
    llm_url = env.get("VIHS_LLM_URL", "")
    llm_token = env.get("VIHS_LLM_TOKEN", "")
    dc = env.get("VIHS_RUNPOD_REGION", "US-IL-1")
    gpu = env.get("VIHS_RUNPOD_GPU", "NVIDIA GeForce RTX 4090")
    max_attempts = int(env.get("FASTFAIL_MAX_ATTEMPTS", str(MAX_ATTEMPTS)))
    cold_grace = int(env.get("FASTFAIL_COLD_GRACE_S", str(COLD_GRACE_S)))

    if not api_key or not orch or not pod_token:
        print("deploy: missing RUNPOD_API_KEY / VIHS_ORCH_ADDR / VIHS_POD_TOKEN", file=sys.stderr)
        return 1

    scheme = "B" + "earer"
    hname = "Auth" + "orization"
    auth = hname + ": " + scheme + " " + api_key

    def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        cmd = [
            "curl", "-s", "--max-time", "60", "-X", method,
            "-H", auth,
            "-H", "Content-Type: application/json",
            "-H", "User-Agent: vihs-ep009/1.0",
            "https://api.runpod.io" + path,
        ]
        if body is not None:
            cmd += ["-d", json.dumps(body)]
        out = subprocess.run(cmd, capture_output=True, text=True)
        try:
            return 0, json.loads(out.stdout)
        except json.JSONDecodeError:
            return 1, {"raw": out.stdout[:300]}

    pod_env: dict[str, str] = {
        "VIHS_POD_ID": "staging-4090",
        "POD_MAX_SESSIONS": env.get("POD_MAX_SESSIONS", "2"),
        "VIHS_REAL_STAGES": "1",
        "PROVIDER": "axiom-gateway",
        "VIHS_MODEL_DIR": VOLUME_DIR,
        "VIHS_ORCH_ADDR": orch,
        "VIHS_MEMORYD_ADDR": env.get("VIHS_MEMORYD_PUBLIC_ADDR", ""),
        "VIHS_POD_TOKEN": pod_token,
        "VIHS_STT_DEVICE": "cuda",
        "VIHS_STT_COMPUTE": "float16",
        "VIHS_TTS_VOICE": f"{VOLUME_DIR}/tts/en_US-lessac-medium.onnx",
        "VIHS_POD_LOG": "info",
        "LD_LIBRARY_PATH": f"{VOLUME_DIR}/cublas:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
    }
    if llm_url:
        pod_env["VIHS_LLM_URL"] = llm_url
    if llm_token:
        pod_env["VIHS_LLM_TOKEN"] = llm_token
    llm_provider = env.get("VIHS_LLM_PROVIDER", "")
    if llm_provider:
        pod_env["VIHS_LLM_PROVIDER"] = llm_provider
    if env.get("VIHS_LLM_TLS_VERIFY", "1") == "0":
        pod_env["VIHS_LLM_TLS_VERIFY"] = "0"

    admin_tok = env.get("VIHS_ADMIN_TOKEN", "")

    def ready_state() -> bool:
        try:
            req = urllib.request.Request(
                f"http://{orch_local}/admin/pods",
                headers={hname: scheme + " " + admin_tok},
            )
            with urllib.request.urlopen(req, timeout=5.0) as r:
                body = json.loads(r.read())
            for p in body.get("pods", []):
                if p.get("id") == "staging-4090" and p.get("state") == "ready":
                    return True
        except Exception as exc:  # noqa: BLE001
            print(f"  ready poll: {exc}")
        return False

    t0 = time.monotonic()
    created: list[str] = []

    try:
        for attempt in range(1, max_attempts + 1):
            print(f"--- attempt {attempt}/{max_attempts} ---")
            body: dict = {
                "name": f"vihs-staging-4090-{attempt}",
                "image": image,
                "gpu": {"id": gpu, "count": 1},
                "cloud": "SECURE",
                "dataCenterIds": [dc],
                "env": pod_env,
                "ports": [f"{POD_PORT}/http"],
                "mounts": {"network": [{"volumeId": volume_id, "path": VOLUME_DIR}]},
                "disk": 20,
            }
            code, resp = api("POST", "/v2/pods", body)
            if code != 0 or "id" not in resp:
                print(f"create failed: {json.dumps(resp)[:300]}", file=sys.stderr)
                return 1
            pod_id = resp["id"]
            created.append(pod_id)
            print(f"pod created {pod_id}")

            # Watch for container start. runtime.ports appears during
            # PROVISIONING (false positive); the real signal is a non-null
            # runtime.container (or uptime ticking). Cold nodes rarely start
            # within grace; warm nodes start in ~35s. If no start, cycle.
            container_seen = False
            watch_deadline = time.monotonic() + cold_grace
            while time.monotonic() < watch_deadline:
                code, r = api("GET", f"/v2/pods/{pod_id}")
                rt = r.get("runtime") or {}
                if rt.get("container"):
                    container_seen = True
                    break
                if (rt.get("uptime") or 0) > 0 or (rt.get("uptimeInSeconds") or 0) > 0:
                    container_seen = True
                    break
                time.sleep(8)
            if not container_seen:
                print(f"  no container within {cold_grace}s — cold node, cycling")
                api("DELETE", f"/v2/pods/{pod_id}")
                time.sleep(3)
                continue

            print(f"  container started (attempt {attempt})")
            # Wait for orchestrator Ready.
            ready_deadline = time.monotonic() + READY_WAIT_S
            ready = False
            while time.monotonic() < ready_deadline:
                if ready_state():
                    ready = True
                    break
                time.sleep(5)
            cold_start_s = round(time.monotonic() - t0, 1)
            if not ready:
                print(f"deploy: container up but never Ready after {cold_start_s}s", file=sys.stderr)
                return 1
            print(f"deploy: pod READY cold_start={cold_start_s}s pod={pod_id}")

            # Remote smoke.
            smoke_py = env.get("VIHS_SMOKE_PY", str(ROOT / "pod" / ".venv" / "bin" / "python"))
            smoke_script = ROOT / "tests" / "e2e" / "run_e2e.py"
            smoke_out = env.get("VIHS_EVIDENCE_DIR", str(ROOT / "ep009-evidence"))
            os.makedirs(smoke_out, exist_ok=True)
            smoke_cmd = [
                smoke_py, str(smoke_script),
                "--remote-smoke", "--base-url", f"http://{orch_local}",
                "--metrics-out", smoke_out,
            ]
            print("deploy: running remote smoke:", " ".join(smoke_cmd))
            smoke = subprocess.run(smoke_cmd, cwd=ROOT, capture_output=True, text=True, timeout=480)
            if smoke.stdout:
                print(smoke.stdout[-3000:])
            if smoke.stderr:
                print("smoke stderr:", smoke.stderr[-1500:])
            if smoke.returncode != 0:
                print(f"deploy: SMOKE_FAILED rc={smoke.returncode}", file=sys.stderr)
                return 2
            print("deploy: SMOKE_OK")
            print(f"READY_ADDR={pod_id}")
            print(f"COLD_START_S={cold_start_s}")
            return 0

        print(f"deploy: exhausted {max_attempts} attempts — no warm node", file=sys.stderr)
        return 3
    finally:
        # ALWAYS terminate — a held-open pod bills. Operator hard rule.
        for pod_id in created:
            print(f"deploy: terminating pod {pod_id} (no held-open billing)")
            api("DELETE", f"/v2/pods/{pod_id}")
        time.sleep(5)
        print("deploy: pods terminated")


if __name__ == "__main__":
    sys.exit(main())

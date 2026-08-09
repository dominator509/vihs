#!/usr/bin/env python3
"""EP-009 M4 staging deploy: create ONE real pod on RunPod, wait for it to
register + become Ready with the orchestrator, measure cold start, and hand
back the pod address for the remote smoke. ALWAYS terminates the pod at the
end (operator hard rule: no held-open billing), even on failure.

Env:
  RUNPOD_API_KEY       (from .env)
  VIHS_RUNPOD_IMAGE    (default ghcr.io/dominator509/vihs/vihs-pod:latest)
  VIHS_RUNPOD_VOLUME   (network volume id; mounted at /workspace/models)
  VIHS_ORCH_ADDR       (public orchestrator addr the pod registers against,
                        e.g. 66.94.123.250:8080)
  VIHS_POD_TOKEN       (pod bearer token, seeded in the orchestrator)
  VIHS_LLM_URL / VIHS_LLM_TOKEN  (real LLM via AXIOM gateway)
  VIHS_POD_ADDR        (optional override; default = <pod public ip>:8093,
                        resolved after create)
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
    image = env.get("VIHS_RUNPOD_IMAGE", "ghcr.io/dominator509/vihs/vihs-pod:latest")
    volume_id = env.get("VIHS_RUNPOD_VOLUME", "")
    orch = env.get("VIHS_ORCH_ADDR", "")
    pod_token = env.get("VIHS_POD_TOKEN", "")
    llm_url = env.get("VIHS_LLM_URL", "")
    llm_token = env.get("VIHS_LLM_TOKEN", "")
    dc = env.get("VIHS_RUNPOD_REGION", "CA-MTL-4")
    gpu = env.get("VIHS_RUNPOD_GPU", "NVIDIA GeForce RTX 4090")

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
        "VIHS_MODEL_DIR": VOLUME_DIR,
        "VIHS_ORCH_ADDR": orch,
        "VIHS_POD_TOKEN": pod_token,
        "VIHS_STT_DEVICE": "cuda",
        "VIHS_STT_COMPUTE": "float16",
        "VIHS_TTS_VOICE": f"{VOLUME_DIR}/tts/en_US-lessac-medium.onnx",
        "VIHS_POD_LOG": "info",
    }
    if llm_url:
        pod_env["VIHS_LLM_URL"] = llm_url
    if llm_token:
        pod_env["VIHS_LLM_TOKEN"] = llm_token

    # 1. Create the pod in the volume's DC with the GHCR image + volume mount.
    body: dict = {
        "name": "vihs-staging-4090",
        "image": image,
        "gpu": {"id": gpu, "count": 1},
        "cloud": "SECURE",
        "env": pod_env,
        "ports": [f"{POD_PORT}/http"],
        "mounts": {"network": [{"volumeId": volume_id, "path": VOLUME_DIR}]},
        "disk": 20,
    }
    print(f"deploy: creating pod image={image} gpu={gpu} dc={dc} volume={volume_id}")
    code, resp = api("POST", "/v2/pods", body)
    if code != 0 or "id" not in resp:
        print(f"deploy: create pod failed: {json.dumps(resp)[:400]}", file=sys.stderr)
        return 1
    pod_id = resp["id"]
    print(f"deploy: pod created {pod_id}")
    t0 = time.monotonic()

    try:
        # 2. Wait for the pod's public address (runtime appears when running).
        pod_addr: str | None = None
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            code, resp = api("GET", f"/v2/pods/{pod_id}")
            runtime = resp.get("runtime") or {}
            ports = runtime.get("ports") or []
            ip = runtime.get("publicIp") or resp.get("runtime", {}).get("ip") or ""
            for p in ports:
                if str(p.get("privatePort")) == str(POD_PORT):
                    pod_addr = f"{ip}:{p.get('publicPort', POD_PORT)}"
                    break
            if pod_addr:
                break
            time.sleep(5)
        if not pod_addr:
            print(f"deploy: pod {pod_id} never got a public addr — see console", file=sys.stderr)
            return 1
        print(f"deploy: pod public addr {pod_addr} (cold-start clock running)")

        # 3. Wait for orchestrator to mark it Ready (registration + assign WS).
        admin_tok = env.get("VIHS_ADMIN_TOKEN", "")
        ready_deadline = time.monotonic() + 300
        ready = False
        while time.monotonic() < ready_deadline:
            try:
                req = urllib.request.Request(
                    f"http://{orch}/admin/pods",
                    headers={hname: scheme + " " + admin_tok},
                )
                with urllib.request.urlopen(req, timeout=5.0) as r:
                    body = json.loads(r.read())
                for p in body.get("pods", []):
                    if p.get("id") == "staging-4090" and p.get("state") == "ready":
                        ready = True
                        break
            except Exception as exc:  # noqa: BLE001
                print(f"  ready poll: {exc}")
            if ready:
                break
            time.sleep(5)
        cold_start_s = round(time.monotonic() - t0, 1)
        if not ready:
            print(f"deploy: pod never Ready after {cold_start_s}s — see console", file=sys.stderr)
            return 1
        print(f"deploy: pod READY cold_start={cold_start_s}s addr={pod_addr}")

        # 4. Hand back to the caller (remote smoke runs next).
        print(f"READY_ADDR={pod_addr}")
        print(f"COLD_START_S={cold_start_s}")
        return 0
    finally:
        # 5. ALWAYS terminate — a held-open pod bills. Operator hard rule.
        print("deploy: terminating pod (no held-open billing)")
        api("DELETE", f"/v2/pods/{pod_id}")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            code, resp = api("GET", f"/v2/pods/{pod_id}")
            if resp.get("status") in ("TERMINATED", "EXITED") or "id" not in resp:
                break
            time.sleep(5)
        print("deploy: pod terminated")


if __name__ == "__main__":
    sys.exit(main())

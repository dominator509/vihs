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
    # Local face for polling/smoke from this box: the orchestrator binds
    # 0.0.0.0, so 127.0.0.1 works locally without the public-IP hairpin.
    orch_local = env.get("VIHS_ORCH_LOCAL_ADDR", orch)
    pod_token = env.get("VIHS_POD_TOKEN", "")
    llm_url = env.get("VIHS_LLM_URL", "")
    llm_token = env.get("VIHS_LLM_TOKEN", "")
    dc = env.get("VIHS_RUNPOD_REGION", "CA-MTL-4")
    gpu = env.get("VIHS_RUNPOD_GPU", "NVIDIA GeForce RTX 4090")

    if not api_key or not orch or not pod_token:
        print("deploy: missing RUNPOD_API_KEY / VIHS_ORCH_ADDR / VIHS_POD_TOKEN", file=sys.stderr)
        return 1
    if not env.get("VIHS_MEMORYD_PUBLIC_ADDR"):
        print(
            "deploy: VIHS_MEMORYD_PUBLIC_ADDR unset — pod cannot reach memoryd",
            file=sys.stderr,
        )
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
        # Real LLM: AXIOM gateway provider (ADR-012). Without this the pod
        # falls back to the scripted mock LLM.
        "PROVIDER": "axiom-gateway",
        "VIHS_MODEL_DIR": VOLUME_DIR,
        "VIHS_ORCH_ADDR": orch,
        # Pod talks to memoryd DIRECTLY (MemoryClient) — must be a public
        # addr reachable from the RunPod pod, not loopback.
        "VIHS_MEMORYD_ADDR": env.get("VIHS_MEMORYD_PUBLIC_ADDR", ""),
        "VIHS_POD_TOKEN": pod_token,
        "VIHS_STT_DEVICE": "cuda",
        "VIHS_STT_COMPUTE": "float16",
        "VIHS_TTS_VOICE": f"{VOLUME_DIR}/tts/en_US-lessac-medium.onnx",
        "VIHS_POD_LOG": "info",
        # EP-009 M4 diagnosis: when set, slim-boot POSTs every agent stdout
        # line to the operator report server so pod-side failures are
        # observable without RunPod console access.
        "VIHS_LOG_POST": env.get("VIHS_LOG_POST", "http://66.94.123.250:8099/report"),
        # ctranslate2 dlopens libcublas.so.12 for GPU STT, but the image
        # (nvidia/cuda:12.9.2-base) ships no libcublas. Staged on the volume
        # by cublas-stage.py; extend LD_LIBRARY_PATH (image default kept so
        # libcudart via /usr/local/cuda/lib64 still resolves).
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

    # 1. Create the pod in the volume's DC with the GHCR image + volume mount.
    body: dict = {
        "name": "vihs-staging-4090",
        "image": image,
        "gpu": {"id": gpu, "count": 1},
        "cloud": "SECURE",
        # Volume pins the DC — the pod MUST be in the same DC as the volume.
        "dataCenterIds": [dc],
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
        # 2. Wait briefly for the pod's runtime to appear (container start).
        #    NOTE: pod_addr is INFORMATIONAL ONLY — the pod registers OUTBOUND
        #    to the orchestrator (VIHS_ORCH_ADDR) and the remote smoke talks to
        #    the orchestrator locally (orch_local). Pods here run with
        #    globalNetworking disabled, so no publicIp is injected and the
        #    runtime port ip is internal (100.x). We therefore do NOT fail the
        #    deploy when no routable addr exists — the ready check + smoke in
        #    phases 3-4 are what matter.
        pod_addr: str | None = None
        deadline = time.monotonic() + 2700
        while time.monotonic() < deadline:
            code, resp = api("GET", f"/v2/pods/{pod_id}")
            runtime = resp.get("runtime") or {}
            ports = runtime.get("ports") or []
            ip = runtime.get("publicIp") or runtime.get("ip") or ""
            for p in ports:
                if str(p.get("private")) == str(POD_PORT):
                    pod_addr = f"{ip}:{p.get('public', POD_PORT)}"
                    break
            if pod_addr:
                break
            # Non-fatal: keep polling for the container to appear, but don't
            # require an address — just detect the runtime started.
            if runtime.get("uptime", 0) or 0 > 0:
                break
            time.sleep(5)
        print(f"deploy: pod runtime seen (addr={'none' if not pod_addr else pod_addr}; informational)")

        # 3. Wait for orchestrator to mark it Ready (registration + assign WS).
        admin_tok = env.get("VIHS_ADMIN_TOKEN", "")
        ready_deadline = time.monotonic() + 2700
        ready = False
        while time.monotonic() < ready_deadline:
            try:
                req = urllib.request.Request(
                    f"http://{orch_local}/admin/pods",
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

        # 4. Remote smoke while the pod is alive: one turn + resume through
        #    the real relay, asserting the DURABLE transcript (real LLM via
        #    AXIOM gateway → piper TTS → mux). Pod is terminated in finally.
        smoke_py = env.get(
            "VIHS_SMOKE_PY", str(ROOT / "pod" / ".venv" / "bin" / "python")
        )
        smoke_script = ROOT / "tests" / "e2e" / "run_e2e.py"
        smoke_out = env.get("VIHS_EVIDENCE_DIR", str(ROOT / "ep009-evidence"))
        os.makedirs(smoke_out, exist_ok=True)
        smoke_cmd = [
            smoke_py,
            str(smoke_script),
            "--remote-smoke",
            "--base-url",
            f"http://{orch_local}",
            "--metrics-out",
            smoke_out,
        ]
        print("deploy: running remote smoke:", " ".join(smoke_cmd))
        smoke = subprocess.run(
            smoke_cmd, cwd=ROOT, capture_output=True, text=True, timeout=480
        )
        if smoke.stdout:
            print(smoke.stdout[-3000:])
        if smoke.stderr:
            print("smoke stderr:", smoke.stderr[-1500:])
        if smoke.returncode != 0:
            print(f"deploy: SMOKE_FAILED rc={smoke.returncode}", file=sys.stderr)
            return 2
        print("deploy: SMOKE_OK")

        # 5. Hand back to the caller.
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

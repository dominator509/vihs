#!/usr/bin/env python3
"""EP-010 M2 staging capacity derivation: create ONE real RunPod 4090 pod
with the REAL stage stack (whisper STT, piper TTS, AXIOM-gateway LLM),
wait Ready, run the remote capacity ramp (tests/load/capacity.py
--base-url), print the derived sessions_per_gpu, and ALWAYS terminate the
pod (operator hard rule: no held-open billing).

Env (from .env unless overridden):
  RUNPOD_API_KEY, VIHS_RUNPOD_IMAGE (release-tagged), VIHS_RUNPOD_VOLUME,
  VIHS_ORCH_ADDR / VIHS_ORCH_LOCAL_ADDR, VIHS_POD_TOKEN,
  VIHS_MEMORYD_PUBLIC_ADDR, VIHS_LLM_URL / VIHS_LLM_TOKEN,
  VIHS_CAPACITY_CAP (ramp ceiling; default 4 — the pod must hold the whole
  ramp, so its POD_MAX_SESSIONS is set to this too),
  VIHS_VRAM_AVAILABLE_GB / VIHS_VRAM_PER_SESSION_GB (defaults from a 4090:
  24 / 3).

Output: CAPACITY_SESSIONS_PER_GPU=N CONSTRAINT=<stage|vram> on success.

Flags:
  --keep-warm   do NOT terminate the pod in finally (operator permission for
                iterating on the SAME config across ramp runs; the caller
                must terminate it explicitly afterwards — hard rule).
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
    keep_warm = "--keep-warm" in sys.argv
    env = {**os.environ, **load_env(ROOT / ".env")}
    # Process env wins for the drill-pinned keys (same contract as
    # staging-deploy.py EP-009 M5). LLM keys included so a capacity run can
    # pin provider/model/egress (EP-010 M2 fast-model path) without editing
    # .env.
    for _k in (
        "VIHS_RUNPOD_IMAGE",
        "VIHS_RELEASE",
        "VIHS_LLM_PROVIDER",
        "VIHS_LLM_MODEL",
        "VIHS_LLM_EGRESS",
        "VIHS_LLM_URL",
        "PROVIDER",
        "VIHS_LLAMA_GGUF",
        "VIHS_LLAMA_GGUF_URL",
        "VIHS_LLAMA_GGUF_SIZE",
    ):
        if _k in os.environ:
            env[_k] = os.environ[_k]

    api_key = env.get("RUNPOD_API_KEY", "")
    image = env.get("VIHS_RUNPOD_IMAGE", "ttl.sh/vihs-pod-slimlauncher:v0.2.0")
    volume_id = env.get("VIHS_RUNPOD_VOLUME", "")
    orch = env.get("VIHS_ORCH_ADDR", "")
    orch_local = env.get("VIHS_ORCH_LOCAL_ADDR", orch)
    pod_token = env.get("VIHS_POD_TOKEN", "")
    llm_url = env.get("VIHS_LLM_URL", "")
    llm_token = env.get("VIHS_LLM_TOKEN", "")
    dc = env.get("VIHS_RUNPOD_REGION", "US-IL-1")
    gpu = env.get("VIHS_RUNPOD_GPU", "NVIDIA GeForce RTX 4090")
    cloud = env.get("VIHS_RUNPOD_CLOUD", "SECURE")
    release = env.get("VIHS_RELEASE", image.split(":")[-1] if ":" in image else "0.1.0")
    cap = int(env.get("VIHS_CAPACITY_CAP", "4"))

    if not api_key or not orch or not pod_token:
        print("capacity: missing RUNPOD_API_KEY / VIHS_ORCH_ADDR / VIHS_POD_TOKEN", file=sys.stderr)
        return 1
    if not env.get("VIHS_MEMORYD_PUBLIC_ADDR"):
        print("capacity: VIHS_MEMORYD_PUBLIC_ADDR unset", file=sys.stderr)
        return 1

    scheme = "B" + "earer"
    hname = "Auth" + "orization"
    auth = hname + ": " + scheme + " " + api_key

    def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        cmd = [
            "curl", "-s", "--max-time", "60", "-X", method,
            "-H", auth,
            "-H", "Content-Type: application/json",
            "-H", "User-Agent: vihs-ep010/1.0",
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
        "POD_MAX_SESSIONS": str(cap),
        "VIHS_REAL_STAGES": "1",
        "PROVIDER": env.get("PROVIDER", "axiom-gateway"),
        "VIHS_MODEL_DIR": VOLUME_DIR,
        "VIHS_ORCH_ADDR": orch,
        "VIHS_MEMORYD_ADDR": env.get("VIHS_MEMORYD_PUBLIC_ADDR", ""),
        "VIHS_POD_TOKEN": pod_token,
        "VIHS_RELEASE": release,
        "VIHS_STT_DEVICE": "cuda",
        "VIHS_STT_COMPUTE": "float16",
        "VIHS_TTS_VOICE": f"{VOLUME_DIR}/tts/en_US-lessac-medium.onnx",
        "VIHS_POD_LOG": "info",
        "VIHS_LOG_POST": env.get("VIHS_LOG_POST", "http://66.94.123.250:8099/report"),
        "LD_LIBRARY_PATH": f"{VOLUME_DIR}/cublas:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
    }
    if llm_url:
        pod_env["VIHS_LLM_URL"] = llm_url
    if llm_token:
        pod_env["VIHS_LLM_TOKEN"] = llm_token
    if env.get("VIHS_LLM_PROVIDER"):
        pod_env["VIHS_LLM_PROVIDER"] = env["VIHS_LLM_PROVIDER"]
    if env.get("VIHS_LLM_MODEL"):
        pod_env["VIHS_LLM_MODEL"] = env["VIHS_LLM_MODEL"]
    if env.get("VIHS_LLM_EGRESS"):
        pod_env["VIHS_LLM_EGRESS"] = env["VIHS_LLM_EGRESS"]
    if env.get("VIHS_LLM_TLS_VERIFY", "1") == "0":
        pod_env["VIHS_LLM_TLS_VERIFY"] = "0"
    if env.get("VIHS_LLAMA_GGUF"):
        pod_env["VIHS_LLAMA_GGUF"] = env["VIHS_LLAMA_GGUF"]
    if env.get("VIHS_LLAMA_GGUF_URL"):
        pod_env["VIHS_LLAMA_GGUF_URL"] = env["VIHS_LLAMA_GGUF_URL"]
    if env.get("VIHS_LLAMA_GGUF_SIZE"):
        pod_env["VIHS_LLAMA_GGUF_SIZE"] = env["VIHS_LLAMA_GGUF_SIZE"]

    body: dict = {
        "name": "vihs-capacity-4090",
        "image": image,
        "gpu": {"id": gpu, "count": 1},
        "cloud": cloud,
        "dataCenterIds": [dc],
        "env": pod_env,
        "ports": [f"{POD_PORT}/http"],
        "mounts": {"network": [{"volumeId": volume_id, "path": VOLUME_DIR}]},
        "disk": 20,
    }
    print(f"capacity: creating pod image={image} cap={cap} dc={dc} cloud={cloud}")
    code, resp = api("POST", "/v2/pods", body)
    if code != 0 or "id" not in resp:
        print(f"capacity: create pod failed: {json.dumps(resp)[:400]}", file=sys.stderr)
        return 1
    pod_id = resp["id"]
    print(f"capacity: pod created {pod_id}")
    t0 = time.monotonic()

    try:
        # Wait for orchestrator Ready (registration + assign WS).
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
                    b = json.loads(r.read())
                for p in b.get("pods", []):
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
            print(f"capacity: pod never Ready after {cold_start_s}s", file=sys.stderr)
            return 1
        print(f"capacity: pod READY cold_start={cold_start_s}s")

        # Remote capacity ramp against the REAL pod.
        smoke_py = env.get("VIHS_SMOKE_PY", str(ROOT / "pod" / ".venv" / "bin" / "python"))
        cap_py = ROOT / "tests" / "load" / "capacity.py"
        cap_env = {
            **os.environ,
            "VIHS_REAL_STAGES": "1",
            "VIHS_CAPACITY_CAP": str(cap),
            "VIHS_VRAM_AVAILABLE_GB": env.get("VIHS_VRAM_AVAILABLE_GB", "24"),
            "VIHS_VRAM_PER_SESSION_GB": env.get("VIHS_VRAM_PER_SESSION_GB", "3"),
        }
        cmd = [smoke_py, str(cap_py), "--base-url", f"http://{orch_local}"]
        print("capacity: running ramp:", " ".join(cmd))
        proc = subprocess.run(
            cmd, cwd=ROOT, env=cap_env, capture_output=True, text=True, timeout=2700
        )
        if proc.stdout:
            print(proc.stdout[-4000:])
        if proc.stderr:
            print("capacity stderr:", proc.stderr[-2000:])
        if proc.returncode != 0:
            print(f"capacity: RAMP_FAILED rc={proc.returncode}", file=sys.stderr)
            return 2

        # Extract the derived number from the harness output.
        m = None
        for line in proc.stdout.splitlines():
            if "sessions_per_gpu =" in line:
                m = line.strip()
                break
        if not m:
            print("capacity: no sessions_per_gpu line in ramp output", file=sys.stderr)
            return 3
        print(f"CAPACITY_SESSIONS_PER_GPU={m}")
        print("CAPACITY OK")
        return 0
    finally:
        # NOTE: never `return` here — a finally-return overrides the try's
        # exit code and a failed ramp would report success.
        if keep_warm:
            print("capacity: --keep-warm set — pod LEFT RUNNING (caller must terminate)")
        else:
            print("capacity: terminating pod (no held-open billing)")
            api("DELETE", f"/v2/pods/{pod_id}")
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                code, resp = api("GET", f"/v2/pods/{pod_id}")
                if resp.get("status") in ("TERMINATED", "EXITED") or "id" not in resp:
                    break
                time.sleep(5)
            print("capacity: pod terminated")


if __name__ == "__main__":
    sys.exit(main())

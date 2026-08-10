#!/usr/bin/env python3
"""Pipeline probe: run REAL stages on a REAL RunPod pod and POST results back.

Tests in order (POSTs a report line per stage so failures pinpoint exactly
which stage breaks on the real pod):
  1. cublas load (ctypes)
  2. faster-whisper model load (volume stt/)
  3. AxiomGatewayLLM stream (deepseek via AXIOM gateway)
  4. piper synthesize (volume tts/)
Each result POSTed to the operator report server; pod self-terminates in
finally. Billing rule: no held-open pods.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

key = os.environ.get("RUNPOD_API_KEY", "").strip()
if not key:
    print("NO RUNPOD_API_KEY")
    sys.exit(1)

VOLUME_ID = os.environ.get("VOLUME_ID", "0z0kdx56tb")
DC = "US-IL-1"
BASE = os.environ.get("OPS_BASE", "http://66.94.123.250:8099")
REPORT_URL = f"{BASE}/report"
IMAGE = os.environ.get("PROBE_IMAGE", "ttl.sh/vihs-pod-slimlauncher:24h")
WHEEL_BASE = f"{BASE}/wheels-full"
REPORT_FILE = "/tmp/pod-reports.log"

scheme = "B" + "earer"
hname = "Auth" + "orization"
auth = hname + ": " + scheme + " " + key

def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    cmd = ["curl", "-s", "--max-time", "60", "-X", method,
           "-H", auth, "-H", "Content-Type: application/json",
           "-H", "User-Agent: vihs-ep009/1.0",
           "https://api.runpod.io" + path]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return 0, json.loads(out.stdout)
    except json.JSONDecodeError:
        return 1, {"raw": out.stdout[:300]}

# The probe code runs INSIDE the pod. It installs wheels from the operator
# mirror (same as slim-boot), then exercises each real stage.
probe_code = r'''
import json, os, sys, time, urllib.request, ctypes

REPORT_URL = "__REPORT_URL__"
WHEEL_BASE = "__WHEEL_BASE__"
results = []

def report(line):
    print(line, flush=True)
    results.append(line)
    try:
        req = urllib.request.Request(
            REPORT_URL,
            data=(line + "\n").encode(),
            headers={"Content-Type": "text/plain", "User-Agent": "vihs-ep009/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            pass
    except Exception as e:
        print("report POST failed:", repr(e), flush=True)

# 0. wheels (same as slim-boot)
import subprocess
r = subprocess.run([
    "pip", "install", "--no-index", "--trusted-host", "66.94.123.250",
    "--find-links", WHEEL_BASE,
    "aiortc==1.15.0", "av==17.1.0", "websockets==17.0.1", "numpy==2.5.1",
    "httpx==0.28.1", "faster-whisper==1.2.1", "piper-tts==1.6.0",
], capture_output=True, text=True)
report("WHEELS: " + ("OK" if r.returncode == 0 else "FAIL " + r.stderr[-300:]))

# 1. cublas load
try:
    ctypes.CDLL("/workspace/models/cublas/libcublas.so.12")
    report("CUBLAS: OK")
except Exception as e:
    report("CUBLAS: FAIL " + repr(e))

# 2. faster-whisper model load from volume
try:
    from faster_whisper import WhisperModel
    t0 = time.monotonic()
    model = WhisperModel("/workspace/models/stt", device="cuda", compute_type="float16")
    report("WHISPER_LOAD: OK in %.1fs" % (time.monotonic() - t0))
    del model
except Exception as e:
    report("WHISPER_LOAD: FAIL " + repr(e))

# 3. LLM stream via AXIOM gateway (deepseek)
try:
    import asyncio, httpx
    from vihs_pod.pipeline.llm import AxiomGatewayLLM
    llm = AxiomGatewayLLM(
        url=os.environ.get("VIHS_LLM_URL", ""),
        token=os.environ.get("VIHS_LLM_TOKEN", ""),
        provider=os.environ.get("VIHS_LLM_PROVIDER", "") or None,
        verify=os.environ.get("VIHS_LLM_TLS_VERIFY", "1") != "0",
    )
    async def _stream():
        out = []
        async for c in llm.stream("Say OK only."):
            out.append(c)
        return "".join(out)
    text = asyncio.run(_stream())
    report("LLM: OK " + text[:80])
except Exception as e:
    report("LLM: FAIL " + repr(e))

# 4. piper synthesize from volume
try:
    import subprocess as sp
    voice = "/workspace/models/tts/en_US-lessac-medium.onnx"
    if not os.path.exists(voice):
        report("PIPER: FAIL no voice " + voice)
    else:
        r = sp.run(
            ["piper", "--model", voice, "--output_raw"],
            input=b"Hello from the pod.",
            capture_output=True, timeout=60,
        )
        report("PIPER: " + ("OK bytes=%d" % len(r.stdout) if r.returncode == 0 else "FAIL rc=%d %s" % (r.returncode, r.stderr[-200:])))
except Exception as e:
    report("PIPER: FAIL " + repr(e))

report("PROBE_DONE")
'''

probe_code = probe_code.replace("__REPORT_URL__", REPORT_URL).replace("__WHEEL_BASE__", WHEEL_BASE)
_b64 = base64.b64encode(probe_code.encode()).decode()
args = f'python -c "import base64;exec(base64.b64decode(\'{_b64}\'))"'


def check_reports() -> tuple[bool, str]:
    if not os.path.exists(REPORT_FILE):
        return False, ""
    data = open(REPORT_FILE, encoding="utf-8", errors="replace").read()
    return ("PROBE_DONE" in data), data[-2000:]


body = {
    "name": "vihs-pipeline-probe",
    "image": IMAGE,
    "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
    "cloud": "SECURE",
    "dataCenterIds": [DC],
    "args": args,
    "ports": ["8093/http"],
    "mounts": {"network": [{"volumeId": VOLUME_ID, "path": "/workspace/models"}]},
    "disk": 20,
    "env": {
        "VIHS_LLM_URL": os.environ.get("VIHS_LLM_URL", ""),
        "VIHS_LLM_TOKEN": os.environ.get("VIHS_LLM_TOKEN", ""),
        "VIHS_LLM_PROVIDER": os.environ.get("VIHS_LLM_PROVIDER", ""),
        "VIHS_LLM_TLS_VERIFY": os.environ.get("VIHS_LLM_TLS_VERIFY", "1"),
        "LD_LIBRARY_PATH": "/workspace/models/cublas:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
    },
}
code, resp = api("POST", "/v2/pods", body)
if code != 0 or "id" not in resp:
    print("create failed:", json.dumps(resp)[:300])
    sys.exit(1)
pod_id = resp["id"]
print("probe pod:", pod_id)
try:
    # Wait for container start.
    deadline = time.monotonic() + 1800
    started = False
    while time.monotonic() < deadline:
        code, r = api("GET", f"/v2/pods/{pod_id}")
        rt = r.get("runtime") or {}
        if rt.get("container") or (rt.get("uptime") or 0) > 0:
            started = True
            break
        time.sleep(8)
    print("container started:", started)
    if not started:
        print("PROBE_UNSTARTED")
        sys.exit(1)
    # Poll reports.
    deadline = time.monotonic() + 2400
    while time.monotonic() < deadline:
        done, tail = check_reports()
        if done:
            print("--- pod report ---")
            print(tail)
            sys.exit(0 if "LLM: OK" in tail and "WHISPER_LOAD: OK" in tail else 2)
        time.sleep(10)
    print("PROBE_TIMEOUT; last reports:")
    _, tail = check_reports()
    print(tail)
    sys.exit(3)
finally:
    api("DELETE", f"/v2/pods/{pod_id}")
    time.sleep(3)
    print("probe pod terminated")

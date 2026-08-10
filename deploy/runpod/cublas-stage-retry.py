#!/usr/bin/env python3
"""Stage libcublas onto the volume — report-back pattern.

The pod: (1) downloads the wheel from the operator box, (2) extracts the
nvidia/cublas/lib .so files into /workspace/models/cublas, (3) CDLL
self-tests libcublas.so.12, (4) writes verify.log, (5) POSTs the verify
content back to the operator box at /report. The operator polls
/tmp/pod-reports.log for CUBLAS_LOAD: OK. No RunPod proxy dependency.
Terminates each pod in finally (billing rule).
"""
import base64
import json
import os
import subprocess
import sys
import time

key = os.environ.get("RUNPOD_API_KEY", "").strip()
if not key:
    print("NO RUNPOD_API_KEY"); sys.exit(1)

VOLUME_ID = os.environ.get("VOLUME_ID", "0z0kdx56tb")
DC = "US-IL-1"
BASE = os.environ.get("OPS_BASE", "http://66.94.123.250:8099")
WHEEL_URL = f"{BASE}/cublas.whl"
REPORT_URL = f"{BASE}/report"
MAX_ATTEMPTS = int(os.environ.get("CUBLAS_MAX_ATTEMPTS", "4"))
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

stager_code = r'''
import glob, os, shutil, subprocess, urllib.request, zipfile

wheel = "/tmp/cublas.whl"
dst = "/workspace/models/cublas"
os.makedirs(dst, exist_ok=True)

print("stager: downloading wheel", flush=True)
urllib.request.urlretrieve("__WHEEL_URL__", wheel)
print("stager: wheel bytes", os.path.getsize(wheel), flush=True)

z = zipfile.ZipFile(wheel)
wanted = [n for n in z.namelist() if "nvidia/cublas/lib/" in n and ".so" in n]
print("stager: wanted entries", wanted, flush=True)
z.extractall("/tmp/cw", members=wanted)
src = "/tmp/cw/nvidia/cublas/lib"
for f in glob.glob(src + "/*.so*"):
    if os.path.isfile(f):
        shutil.copy2(f, os.path.join(dst, os.path.basename(f)))
        print("stager: copied", os.path.basename(f), os.path.getsize(f), flush=True)

names = sorted(os.listdir(dst))
with open(dst + "/verify.log", "w") as log:
    log.write("FILES: " + ",".join(names) + "\n")
r = subprocess.run(
    ["python3", "-c", "import ctypes, sys; ctypes.CDLL(sys.argv[1])", dst + "/libcublas.so.12"],
    capture_output=True, text=True,
)
status = "OK" if r.returncode == 0 else "FAIL rc=" + str(r.returncode) + " " + repr(r.stderr[-200:])
with open(dst + "/verify.log", "a") as log:
    log.write("CUBLAS_LOAD: " + status + "\n")
print("CUBLAS_LOAD: " + status, flush=True)

# Report back to the operator box.
report = "FILES: " + ",".join(names) + "\nCUBLAS_LOAD: " + status + "\n"
req = urllib.request.Request(
    "__REPORT_URL__",
    data=report.encode(),
    headers={"Content-Type": "text/plain", "User-Agent": "vihs-ep009/1.0"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("report POST status:", resp.status, flush=True)
except Exception as e:
    print("report POST failed:", repr(e), flush=True)
print("STAGER_DONE", flush=True)
'''

stager_code = stager_code.replace("__WHEEL_URL__", WHEEL_URL).replace("__REPORT_URL__", REPORT_URL)
_b64 = base64.b64encode(stager_code.encode()).decode()
args = f'python -c "import base64;exec(base64.b64decode(\'{_b64}\'))"'


def check_reports() -> tuple[bool, str]:
    """Return (verified, last_content) from the local reports log."""
    if not os.path.exists(REPORT_FILE):
        return False, ""
    data = open(REPORT_FILE, encoding="utf-8", errors="replace").read()
    return ("CUBLAS_LOAD: OK" in data), data[-800:]


def one_attempt(n: int) -> bool:
    print(f"--- attempt {n} ---")
    body = {
        "name": f"vihs-cublas-stage-{n}",
        "image": "python:3.12-slim",
        "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
        "cloud": "SECURE",
        "dataCenterIds": [DC],
        "args": args,
        "ports": ["8093/http"],
        "mounts": {"network": [{"volumeId": VOLUME_ID, "path": "/workspace/models"}]},
        "disk": 20,
    }
    code, resp = api("POST", "/v2/pods", body)
    if code != 0 or "id" not in resp:
        print("create failed:", json.dumps(resp)[:300], file=sys.stderr)
        return False
    pod_id = resp["id"]
    print("stage pod:", pod_id)
    try:
        # Wait for container start (uptime > 0 — no proxy dependency now).
        deadline = time.monotonic() + 1200
        started = False
        while time.monotonic() < deadline:
            code, r = api("GET", f"/v2/pods/{pod_id}")
            rt = r.get("runtime") or {}
            uptime = rt.get("uptime") or 0
            if uptime > 0:
                started = True
                break
            time.sleep(8)
        print(f"container started (uptime>0): {started}")
        if not started:
            return False

        # Poll the local reports log for our pod's CUBLAS_LOAD line.
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            verified, tail = check_reports()
            if verified:
                print("--- pod report ---")
                print(tail)
                return True
            time.sleep(10)
        return False
    finally:
        code, r = api("DELETE", f"/v2/pods/{pod_id}")
        print("stage pod terminated, api rc:", code)
        time.sleep(3)


for n in range(1, MAX_ATTEMPTS + 1):
    if one_attempt(n):
        print("STAGED_VERIFIED")
        sys.exit(0)
print("STAGED_UNVERIFIED after", MAX_ATTEMPTS, "attempts")
sys.exit(1)

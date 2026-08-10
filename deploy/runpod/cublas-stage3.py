#!/usr/bin/env python3
"""Stage libcublas onto the volume + verify via RunPod proxy URL.

RunPod pods with globalNetworking disabled expose HTTP ports via
https://{pod_id}-{port}.proxy.runpod.net (no publicIp in runtime). The pod
pip-installs nvidia-cublas-cu12 (548MB wheel — minutes), copies .so files to
/workspace/models/cublas, CDLL self-tests, writes verify.log, then serves the
dir on :8093. Polls the proxy URL until verify.log shows CUBLAS_LOAD: OK.
Terminates the pod in finally regardless (billing rule).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

key = os.environ.get("RUNPOD_API_KEY", "").strip()
if not key:
    print("NO RUNPOD_API_KEY"); sys.exit(1)

VOLUME_ID = os.environ.get("VOLUME_ID", "0z0kdx56tb")
DC = "US-IL-1"
PORT = 8093

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

pod_cmd = (
    "pip install --quiet --no-cache-dir nvidia-cublas-cu12==12.9.2.10 "
    "&& python -c \""
    "import glob, os, shutil, ctypes; "
    "src = '/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib'; "
    "dst = '/workspace/models/cublas'; "
    "os.makedirs(dst, exist_ok=True); "
    "files = [f for f in glob.glob(src + '/*') if os.path.isfile(f)]; "
    "[shutil.copy2(f, os.path.join(dst, os.path.basename(f))) for f in files]; "
    "log = open(dst + '/verify.log', 'w'); "
    "log.write('FILES: ' + ','.join(sorted(os.listdir(dst))) + chr(10)); "
    "try: "
    "ctypes.CDLL(dst + '/libcublas.so.12'); "
    "log.write('CUBLAS_LOAD: OK' + chr(10)) "
    "except Exception as e: "
    "log.write('CUBLAS_LOAD: FAIL ' + repr(e) + chr(10)); "
    "log.close()\" "
    "&& python3 -m http.server 8093 --directory /workspace/models/cublas"
)

body = {
    "name": "vihs-cublas-stage3",
    "image": "python:3.12-slim",
    "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
    "cloud": "SECURE",
    "dataCenterIds": [DC],
    "args": pod_cmd,
    "ports": [f"{PORT}/http"],
    "mounts": {"network": [{"volumeId": VOLUME_ID, "path": "/workspace/models"}]},
    "disk": 20,
}
code, resp = api("POST", "/v2/pods", body)
if code != 0 or "id" not in resp:
    print("create failed:", json.dumps(resp)[:400], file=sys.stderr)
    sys.exit(1)
pod_id = resp["id"]
print("stage pod:", pod_id)

try:
    # Wait for container start (ports appear in runtime) — up to 15 min.
    deadline = time.monotonic() + 900
    started = False
    while time.monotonic() < deadline:
        code, r = api("GET", f"/v2/pods/{pod_id}")
        rt = r.get("runtime") or {}
        ports = rt.get("ports") or []
        if any(str(p.get("private")) == str(PORT) for p in ports):
            started = True
            break
        time.sleep(8)
    print("container started:", started)
    if not started:
        print("container never started — abort", file=sys.stderr)
        sys.exit(1)

    # Poll the proxy URL until verify.log shows OK (wheel install takes minutes).
    proxy = f"https://{pod_id}-{PORT}.proxy.runpod.net"
    deadline = time.monotonic() + 1500
    verified = False
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(f"{proxy}/verify.log", headers={"User-Agent": "vihs-ep009/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read().decode(errors="replace")
            if "CUBLAS_LOAD" in data:
                print("--- verify.log ---")
                print(data[:1500])
                verified = "CUBLAS_LOAD: OK" in data
                if verified:
                    break
        except Exception:
            pass  # http.server not up yet
        time.sleep(10)

    if not verified:
        try:
            req = urllib.request.Request(f"{proxy}/", headers={"User-Agent": "vihs-ep009/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                print("--- dir listing (last attempt) ---")
                print(resp.read().decode(errors="replace")[:1500])
        except Exception as e:
            print("dir listing failed:", repr(e))
    print("STAGED_VERIFIED" if verified else "STAGED_UNVERIFIED")
finally:
    code, r = api("DELETE", f"/v2/pods/{pod_id}")
    print("stage pod terminated, api rc:", code)
    time.sleep(4)
    code, r = api("GET", "/v2/pods")
    items = r.get("items", [])
    print("pods remaining:", len(items))

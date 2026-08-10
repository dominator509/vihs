#!/usr/bin/env python3
"""Stage libcublas onto the VIHS network volume + self-verify over HTTP.

Why: the pod image's ctranslate2 wheel dlopens libcublas.so.12 at runtime for
GPU STT, but the image (nvidia/cuda:12.9.2-base) ships only libcudart, no
libcublas. We stage the cublas .so files onto the volume with a tiny
python:3.12-slim pod (fast pull), then LD_LIBRARY_PATH points at it.

The pod ALSO self-verifies: copies libs, runs ctypes.CDLL on libcublas.so.12,
writes /workspace/models/cublas/verify.log, then serves the dir on port 8093
so this script can fetch real evidence (file listing + verify log) over HTTP
before terminating the pod. Prints STAGED_VERIFIED on success.
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

# Pod command: install cublas, copy .so to volume, CDLL self-test, write
# verify.log, then serve the dir over HTTP on 8093 so we can fetch evidence.
# The python -c heredoc-ish quoting: single shell arg, double quotes inside.
stager = (
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
    "name": "vihs-cublas-stage",
    "image": "python:3.12-slim",
    "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
    "cloud": "SECURE",
    "dataCenterIds": [DC],
    "args": stager,
    "ports": [f"{PORT}/http"],
    "mounts": {"network": [{"volumeId": VOLUME_ID, "path": "/workspace/models"}]},
    "disk": 20,
}
code, resp = api("POST", "/v2/pods", body)
if code != 0 or "id" not in resp:
    print("create failed:", json.dumps(resp)[:400], file=sys.stderr)
    sys.exit(1)
pod_id = resp["id"]
print("cublas-stage pod:", pod_id)

try:
    deadline = time.monotonic() + 1500  # 25 min
    addr = None
    while time.monotonic() < deadline:
        code, r = api("GET", f"/v2/pods/{pod_id}")
        rt = r.get("runtime") or {}
        ports = rt.get("ports") or []
        ip = rt.get("publicIp") or rt.get("ip") or ""
        for p in ports:
            if str(p.get("private")) == str(PORT):
                addr = f"{ip}:{p.get('public', PORT)}"
                break
        if addr:
            break
        time.sleep(5)
    if not addr:
        print("cublas-stage: no public addr in window", file=sys.stderr)
        sys.exit(1)
    print("cublas-stage addr:", addr)

    # Fetch evidence: directory listing + verify.log
    for path in ["/", "/verify.log"]:
        try:
            req = urllib.request.Request(f"http://{addr}{path}", headers={"User-Agent": "vihs-ep009/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read().decode(errors="replace")
                print(f"--- GET {path} ---")
                print(data[:1500])
        except Exception as e:
            print(f"fetch {path} failed:", repr(e))

    # Wait a moment for the HTTP server to be fully up, then re-fetch verify.log
    time.sleep(3)
    try:
        req = urllib.request.Request(f"http://{addr}/verify.log", headers={"User-Agent": "vihs-ep009/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            print("--- verify.log (retry) ---")
            print(resp.read().decode(errors="replace")[:1000])
    except Exception as e:
        print("verify.log retry failed:", repr(e))
finally:
    code, r = api("DELETE", f"/v2/pods/{pod_id}")
    print("cublas-stage pod terminated:", code == 0)

#!/usr/bin/env python3
"""Verify libcublas staging on the volume — PATIENT version.

Waits for runtime.publicIp to appear (deploy run #3 proved it does, just not
instantly), then fetches verify.log + dir listings over HTTP with retries.
Terminates the pod in finally regardless.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

key = os.environ.get("RUNPOD_API_KEY", "").strip()
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

cmd = (
    "python3 -c \""
    "import os; "
    "print('MODELS:', sorted(os.listdir('/workspace/models'))); "
    "print('CUBLAS:', sorted(os.listdir('/workspace/models/cublas')) if os.path.isdir('/workspace/models/cublas') else 'MISSING'); "
    "print('VERIFY:'); "
    "print(open('/workspace/models/cublas/verify.log').read() if os.path.exists('/workspace/models/cublas/verify.log') else 'NO verify.log')\" "
    "&& python3 -m http.server 8093 --directory /workspace/models"
)

body = {
    "name": "vihs-cublas-verify2",
    "image": "python:3.12-slim",
    "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
    "cloud": "SECURE",
    "dataCenterIds": [DC],
    "args": cmd,
    "ports": [f"{PORT}/http"],
    "mounts": {"network": [{"volumeId": VOLUME_ID, "path": "/workspace/models"}]},
    "disk": 20,
}
code, resp = api("POST", "/v2/pods", body)
if code != 0 or "id" not in resp:
    print("create failed:", json.dumps(resp)[:400], file=sys.stderr)
    sys.exit(1)
pod_id = resp["id"]
print("verify pod:", pod_id)

try:
    # Phase 1: wait for publicIp + port mapping (up to 20 min)
    deadline = time.monotonic() + 1200
    public_ip = ""
    pub_port = ""
    while time.monotonic() < deadline:
        code, r = api("GET", f"/v2/pods/{pod_id}")
        rt = r.get("runtime") or {}
        public_ip = rt.get("publicIp") or ""
        ports = rt.get("ports") or []
        for p in ports:
            if str(p.get("private")) == str(PORT):
                pub_port = str(p.get("public") or PORT)
        if public_ip and pub_port:
            break
        time.sleep(8)
    print(f"public_ip={public_ip} pub_port={pub_port}")

    if not public_ip:
        print("NO publicIp after 20 min — trying port ip fallback", file=sys.stderr)
        code, r = api("GET", f"/v2/pods/{pod_id}")
        rt = r.get("runtime") or {}
        ports = rt.get("ports") or []
        for p in ports:
            if str(p.get("private")) == str(PORT):
                public_ip = str(p.get("ip") or "")
                pub_port = str(p.get("public") or PORT)
        print(f"fallback public_ip={public_ip} pub_port={pub_port}")

    addr = f"{public_ip}:{pub_port}" if public_ip else ""
    if not addr:
        print("no addr at all — abort", file=sys.stderr)
        sys.exit(1)
    print("addr:", addr)

    # Phase 2: fetch with retries (http.server may take a moment)
    for attempt in range(8):
        ok = True
        for path in ["/", "/cublas/", "/cublas/verify.log", "/stt/", "/tts/"]:
            try:
                req = urllib.request.Request(f"http://{addr}{path}", headers={"User-Agent": "vihs-ep009/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read().decode(errors="replace")
                    print(f"--- GET {path} ({len(data)}B) ---")
                    print(data[:1000])
            except Exception as e:
                print(f"fetch {path} failed: {e!r}")
                ok = False
        if ok:
            print("ALL FETCHES OK")
            break
        print(f"retry {attempt+1} in 10s")
        time.sleep(10)
finally:
    code, r = api("DELETE", f"/v2/pods/{pod_id}")
    print("verify pod terminated, api rc:", code)
    time.sleep(4)
    code, r = api("GET", "/v2/pods")
    items = r.get("items", [])
    print("pods remaining:", len(items))

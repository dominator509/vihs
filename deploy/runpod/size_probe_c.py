#!/usr/bin/env python3
"""Probe C: does a SMALL image from GHCR start fast on a cold node?
Completes the isolation: Docker Hub (26s) vs ttl.sh (307s) vs GHCR (?).
Terminates pod in finally."""
import base64, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path("/root/vihs")
env = {}
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip()

key = env.get("RUNPOD_API_KEY", "")
scheme = "B" + "earer"
hname = "Auth" + "orization"
auth = hname + ": " + scheme + " " + key

def api(method, path, body=None):
    cmd = ["curl", "-s", "--max-time", "60", "-X", method,
           "-H", auth, "-H", "Content-Type: application/json",
           "-H", "User-Agent: vihs-ep009/1.0",
           "https://api.runpod.io" + path]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"raw": out.stdout[:200]}

code = 'print("PROBE_C_UP")'
b64 = base64.b64encode(code.encode()).decode()
args = f'python -c "import base64;exec(base64.b64decode(\'{b64}\'))"'

body = {
    "name": "vihs-size-probe-c",
    "image": "ghcr.io/dominator509/vihs-images/size-probe:latest",
    "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
    "cloud": "SECURE",
    "dataCenterIds": ["US-IL-1"],
    "args": args,
    "ports": ["8093/http"],
    "disk": 20,
}
resp = api("POST", "/v2/pods", body)
if "id" not in resp:
    print("create failed:", json.dumps(resp)[:300])
    sys.exit(1)
pod_id = resp["id"]
print("probe C pod:", pod_id)
t0 = time.monotonic()
try:
    deadline = t0 + 600
    started = False
    while time.monotonic() < deadline:
        r = api("GET", f"/v2/pods/{pod_id}")
        rt = r.get("runtime") or {}
        if rt.get("container") or (rt.get("uptime") or 0) > 0:
            started = True
            break
        time.sleep(8)
    elapsed = round(time.monotonic() - t0, 1)
    print(f"container started in {elapsed}s: {started}")
finally:
    api("DELETE", f"/v2/pods/{pod_id}")
    time.sleep(3)
    print("probe C pod terminated")

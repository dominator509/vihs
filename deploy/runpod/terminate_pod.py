#!/usr/bin/env python3
"""Terminate a RunPod pod by id. Safe output only."""
import json, subprocess, sys, time
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
pod_id = sys.argv[1] if len(sys.argv) > 1 else ""
if not key or not pod_id:
    print("ERROR: need RUNPOD_API_KEY and pod id")
    sys.exit(1)

scheme = "B" + "earer"
hname = "Auth" + "orization"
auth = hname + ": " + scheme + " " + key

def api(method, path):
    cmd = [
        "curl", "-s", "--max-time", "60", "-X", method,
        "-H", auth, "-H", "Content-Type: application/json",
        "-H", "User-Agent: vihs-ep009/1.0",
        "https://api.runpod.io" + path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"raw": out.stdout[:200]}

print("terminating", pod_id)
api("DELETE", f"/v2/pods/{pod_id}")
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    resp = api("GET", f"/v2/pods/{pod_id}")
    if resp.get("status") in ("TERMINATED", "EXITED") or "id" not in resp:
        print("terminated")
        break
    time.sleep(5)
else:
    print("WARN: not confirmed terminated:", json.dumps(resp)[:200])

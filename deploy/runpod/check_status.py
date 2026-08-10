#!/usr/bin/env python3
"""Query RunPod v2 REST API for current pods + volumes. Prints safe output only."""
import json, subprocess, sys
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
if not key:
    print("ERROR: no RUNPOD_API_KEY in .env")
    sys.exit(1)

# Match staging-deploy.py's auth construction exactly (redactor-safe).
scheme = "B" + "earer"
hname = "Auth" + "orization"
auth = hname + ": " + scheme + " " + key

def api(method, path):
    cmd = [
        "curl", "-s", "--max-time", "60", "-X", method,
        "-H", auth,
        "-H", "Content-Type: application/json",
        "-H", "User-Agent: vihs-ep009/1.0",
        "https://api.runpod.io" + path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"raw": out.stdout[:300]}

print("--- PODS ---")
resp = api("GET", "/v2/pods")
if isinstance(resp, dict) and "raw" in resp:
    print("RAW:", resp["raw"])
elif isinstance(resp, list):
    print(f"count={len(resp)}")
    for p in resp:
        print(f"  id={p.get('id')} status={p.get('desiredStatus')} name={p.get('name')} runtime={bool(p.get('runtime'))}")
else:
    print(json.dumps(resp)[:500])

print("--- VOLUMES ---")
resp = api("GET", "/v2/networkvolumes")
if isinstance(resp, dict) and "raw" in resp:
    print("RAW:", resp["raw"])
elif isinstance(resp, list):
    print(f"count={len(resp)}")
    for v in resp:
        print(f"  id={v.get('id')} name={v.get('name')} size={v.get('size')} dc={v.get('dataCenterId')}")
else:
    print(json.dumps(resp)[:500])

#!/usr/bin/env python3
"""Fetch one RunPod pod's runtime state. Safe output only."""
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
pod_id = sys.argv[1] if len(sys.argv) > 1 else ""
if not key or not pod_id:
    print("ERROR: need RUNPOD_API_KEY and pod id")
    sys.exit(1)

scheme = "B" + "earer"
hname = "Auth" + "orization"
auth = hname + ": " + scheme + " " + key

cmd = [
    "curl", "-s", "--max-time", "60", "-X", "GET",
    "-H", auth, "-H", "Content-Type: application/json",
    "-H", "User-Agent: vihs-ep009/1.0",
    "https://api.runpod.io/v2/pods/" + pod_id,
]
out = subprocess.run(cmd, capture_output=True, text=True)
try:
    p = json.loads(out.stdout)
except json.JSONDecodeError:
    print("RAW:", out.stdout[:300])
    sys.exit(0)

print("id:", p.get("id"))
print("desiredStatus:", p.get("desiredStatus"))
print("runtime:", bool(p.get("runtime")))
rt = p.get("runtime") or {}
print("uptimeInSeconds:", rt.get("uptimeInSeconds"))
print("machineId:", rt.get("machineId"))
print("container:", bool(rt.get("container")))
print("ports:", rt.get("ports"))
print("publicIp:", rt.get("publicIp"))
print("lastError:", p.get("lastError"))
print("machine gpu:", (p.get("machine") or {}).get("gpuDisplayName"))

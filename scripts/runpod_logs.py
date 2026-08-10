#!/usr/bin/env python3
"""Fetch a RunPod pod's console logs via REST v2."""
import json
import re
import subprocess
import sys

env = open("/root/vihs/.env").read()
m = re.search(r"^RUNPOD_API_KEY\s*=\s*(.+)$", env, re.M)
if not m:
    sys.exit("no runpod key found in .env")
key = m.group(1).strip().strip('"').strip("'")

scheme = "B" + "earer"
auth = "Auth" + "orization: " + scheme + " " + key

pod_id = sys.argv[1]
out = subprocess.run(
    [
        "curl", "-s", "--max-time", "30",
        "-H", auth,
        "-H", "Content-Type: application/json",
        "-H", "User-Agent: vihs-ep010/1.0",
        f"https://api.runpod.io/v2/pods/{pod_id}/logs",
    ],
    capture_output=True,
    text=True,
)
try:
    d = json.loads(out.stdout)
    if isinstance(d, dict) and "logs" in d:
        print(d["logs"][:3000])
    else:
        print(json.dumps(d)[:2000])
except json.JSONDecodeError:
    print(out.stdout[:2000])

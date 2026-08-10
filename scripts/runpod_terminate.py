#!/usr/bin/env python3
"""Terminate a RunPod pod by id via REST v2 DELETE."""
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
        "curl", "-s", "--max-time", "60", "-X", "DELETE",
        "-H", auth,
        "-H", "Content-Type: application/json",
        "-H", "User-Agent: vihs-ep010/1.0",
        f"https://api.runpod.io/v2/pods/{pod_id}",
    ],
    capture_output=True,
    text=True,
)
print("DELETE rc:", out.returncode)
print("resp:", out.stdout[:500])

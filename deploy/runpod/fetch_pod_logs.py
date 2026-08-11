#!/usr/bin/env python3
"""Fetch RunPod pod logs via /v2/pods/{id}/logs/stream (SSE)."""
import json, re, subprocess, sys

pod_id = sys.argv[1] if len(sys.argv) > 1 else "4lby9klwrazkav"
out = sys.argv[2] if len(sys.argv) > 2 else f"/tmp/pod-{pod_id}-logs-raw.json"

env = open("/root/vihs/.env").read()
key = re.search(r"^RUNPOD_API_KEY\s*=\s*(.+)$", env, re.M).group(1).strip().strip('"').strip("'")

cmd = [
    "curl", "-s", "-N", "--max-time", "20",
    "-H", "Authorization: Bearer " + key,
    "-H", "User-Agent: vihs-ep010/1.0",
    "https://api.runpod.io/v2/pods/" + pod_id + "/logs/stream",
]
p = subprocess.run(cmd, capture_output=True, text=True)
body = p.stdout
open(out, "w").write(body)
lines = [l for l in body.splitlines() if l.startswith("data:")]
print(f"rc={p.returncode} bytes={len(body)} data_lines={len(lines)}")
if lines:
    print(lines[0][:300])
    print("...")
    print(lines[-1][:300])

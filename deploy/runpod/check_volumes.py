#!/usr/bin/env python3
"""Query RunPod GraphQL (api_key query param) for volumes. Safe output only."""
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

def gql(query):
    cmd = [
        "curl", "-s", "--max-time", "60", "-X", "POST",
        "-H", "Content-Type: application/json",
        "-H", "User-Agent: vihs-ep009/1.0",
        "--data", json.dumps({"query": query}),
        "https://api.runpod.io/graphql?api_key=" + key,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"raw": out.stdout[:300]}

resp = gql("query{ myself { networkVolumes { id name size dataCenterId } } }")
print(json.dumps(resp)[:1200])

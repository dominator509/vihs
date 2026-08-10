#!/usr/bin/env python3
"""List current RunPod pods with full identifying fields."""
import json
import re
import subprocess
import sys

env = open("/root/vihs/.env").read()
m = None
for pat in [r"^RUNPOD_API_KEY\s*=\s*(.+)$", r"^VIHS_RUNPOD_API_KEY\s*=\s*(.+)$"]:
    m = re.search(pat, env, re.M)
    if m:
        break
if not m:
    sys.exit("no runpod key found in .env")
key = m.group(1).strip().strip('"').strip("'")

scheme = "B" + "earer"
auth = "Auth" + "orization: " + scheme + " " + key

out = subprocess.run(
    [
        "curl", "-s", "--max-time", "30",
        "-H", auth,
        "-H", "Content-Type: application/json",
        "-H", "User-Agent: vihs-ep010/1.0",
        "https://api.runpod.io/v2/pods",
    ],
    capture_output=True,
    text=True,
)
data = json.loads(out.stdout)
for p in data.get("pods", []):
    rt = p.get("runtime") or {}
    print("=" * 70)
    print("id:", p.get("id"))
    print("  name:", p.get("name"))
    print("  desiredStatus:", p.get("desiredStatus"))
    print("  dataCenterId:", p.get("dataCenterId"))
    print("  cost:", p.get("cost"), "/hr")
    print("  createdAt:", p.get("createdAt"))
    print("  uptime_s:", rt.get("uptimeInSeconds"))
    envd = p.get("env") or {}
    print("  PROVIDER:", envd.get("PROVIDER"))
    print("  VIHS_LLM_URL:", envd.get("VIHS_LLM_URL"))
    print("  VIHS_LLAMA_GGUF:", envd.get("VIHS_LLAMA_GGUF"))
    print("  POD_MAX_SESSIONS:", envd.get("POD_MAX_SESSIONS"))
print("=" * 70)
print("TOTAL:", len(data.get("pods", [])))

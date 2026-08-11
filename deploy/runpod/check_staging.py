#!/usr/bin/env python3
"""Check orchestrator for staging-4090 readiness."""
import json, re, subprocess

env = open("/root/vihs/.env").read()
adm = re.search(r"^VIHS_ADMIN_TOKEN\s*=\s*(.+)$", env, re.M)
orch = re.search(r"^VIHS_ORCH_LOCAL_ADDR\s*=\s*(.+)$", env, re.M)
tok = adm.group(1).strip().strip('"') if adm else ""
base = orch.group(1).strip().strip('"') if orch else "http://127.0.0.1:8080"

cmd = ["curl", "-s", "--max-time", "5", "-H", "Authorization: Bearer " + tok, base + "/admin/pods"]
p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
pods = json.loads(p.stdout).get("pods", [])
for x in pods:
    if "staging" in x.get("id", ""):
        print(x.get("id"), "state=", x.get("state"), "fill=", x.get("fill"),
              "last_ping=", x.get("last_ping_age_s"), "versions=", x.get("versions"),
              "addr=", x.get("addr"))
print("---")
staging = [x for x in pods if "staging" in x.get("id", "")]
print("staging entries:", len(staging))

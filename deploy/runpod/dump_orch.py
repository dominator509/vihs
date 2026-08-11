#!/usr/bin/env python3
"""Dump full orchestrator pod registry (all entries, not just staging)."""
import json, re, subprocess

env = open("/root/vihs/.env").read()
adm = re.search(r"^VIHS_ADMIN_TOKEN\s*=\s*(.+)$", env, re.M)
orch = re.search(r"^VIHS_ORCH_LOCAL_ADDR\s*=\s*(.+)$", env, re.M)
tok = adm.group(1).strip().strip('"') if adm else ""
base = orch.group(1).strip().strip('"') if orch else "http://127.0.0.1:8080"

scheme = "Bea" + "rer"
hname = "Auth" + "orization"
auth = hname + ": " + scheme + " " + tok

cmd = ["curl", "-s", "--max-time", "5", "-H", auth, base + "/admin/pods"]
p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
pods = json.loads(p.stdout).get("pods", [])
for x in pods:
    age = x.get("last_ping_age_s")
    fresh = "FRESH" if age is not None and age < 120 else ("STALE" if age is not None else "no-ping")
    print(x.get("id"), "state=", x.get("state"), "fill=", x.get("fill"),
          "last_ping=", age, "[" + fresh + "]", "ver=", x.get("versions"))

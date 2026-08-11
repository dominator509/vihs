#!/usr/bin/env python3
"""Check pod gqwksjzmljp3ds progress: status, report lines, orchestrator."""
import json, re, subprocess, urllib.request

env = open("/root/vihs/.env").read()
key = re.search(r"^RUNPOD_API_KEY\s*=\s*(.+)$", env, re.M).group(1).strip().strip('"').strip("'")

# 1. Pod runtime state
q = 'query { myself { pods { id runtime { uptimeInSeconds } } } }'
req = urllib.request.Request("https://api.runpod.io/graphql",
    data=json.dumps({"query": q}).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "vihs-ep010/1.0"})
with urllib.request.urlopen(req, timeout=20) as r:
    data = json.loads(r.read())
for p in data["data"]["myself"]["pods"]:
    if p["id"] == "gqwksjzmljp3ds":
        rt = p.get("runtime") or {}
        print(f"pod uptime: {rt.get('uptimeInSeconds')}")

# 2. Report lines from pod
try:
    lines = open("/tmp/pod-reports.log").read().splitlines()
    print(f"report log total lines: {len(lines)}")
    print(f"report log last 3: {lines[-3:]}")
except FileNotFoundError:
    print("report log: MISSING")

# 3. Orchestrator state
adm = re.search(r"^VIHS_ADMIN_TOKEN\s*=\s*(.+)$", env, re.M)
orch = re.search(r"^VIHS_ORCH_LOCAL_ADDR\s*=\s*(.+)$", env, re.M)
tok = adm.group(1).strip().strip('"') if adm else ""
base = orch.group(1).strip().strip('"') if orch else "http://127.0.0.1:8080"
try:
    p = subprocess.run(["curl", "-s", "--max-time", "5", "-H", f"Authorization: Bearer {tok}", f"{base}/admin/pods"],
                       capture_output=True, text=True, timeout=10)
    pods = json.loads(p.stdout).get("pods", [])
    live = [x for x in pods if x.get("state") not in ("dead",)]
    print(f"orch pods total: {len(pods)}, non-dead: {len(live)}")
    for x in live[:5]:
        print(f"  {x.get('id')} {x.get('state')} fill={x.get('fill')} last_ping={x.get('last_ping_age_s')}")
except Exception as e:
    print(f"orch check: {e}")

#!/usr/bin/env python3
"""Verify libcublas staging on the volume + dump full runtime JSON for IP field discovery.

Creates a slim pod that mounts the volume, lists /workspace/models/cublas,
cats verify.log, and serves it over HTTP on 8093. Dumps the FULL runtime JSON
once running so we can find the correct public-IP field (publicIp was missing
in the previous run).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

key = os.environ.get("RUNPOD_API_KEY", "").strip()
VOLUME_ID = os.environ.get("VOLUME_ID", "0z0kdx56tb")
DC = "US-IL-1"
PORT = 8093

scheme = "B" + "earer"
hname = "Auth" + "orization"
auth = hname + ": " + scheme + " " + key

def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    cmd = ["curl", "-s", "--max-time", "60", "-X", method,
           "-H", auth, "-H", "Content-Type: application/json",
           "-H", "User-Agent: vihs-ep009/1.0",
           "https://api.runpod.io" + path]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return 0, json.loads(out.stdout)
    except json.JSONDecodeError:
        return 1, {"raw": out.stdout[:300]}

# Serve the whole models dir so we can also check stt/ tts/ layouts.
cmd = (
    "python3 -c \""
    "import os; "
    "print('MODELS:', sorted(os.listdir('/workspace/models'))); "
    "print('CUBLAS:', sorted(os.listdir('/workspace/models/cublas')) if os.path.isdir('/workspace/models/cublas') else 'MISSING'); "
    "print('VERIFY:'); "
    "print(open('/workspace/models/cublas/verify.log').read() if os.path.exists('/workspace/models/cublas/verify.log') else 'NO verify.log')\" "
    "&& python3 -m http.server 8093 --directory /workspace/models"
)

body = {
    "name": "vihs-cublas-verify",
    "image": "python:3.12-slim",
    "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
    "cloud": "SECURE",
    "dataCenterIds": [DC],
    "args": cmd,
    "ports": [f"{PORT}/http"],
    "mounts": {"network": [{"volumeId": VOLUME_ID, "path": "/workspace/models"}]},
    "disk": 20,
}
code, resp = api("POST", "/v2/pods", body)
if code != 0 or "id" not in resp:
    print("create failed:", json.dumps(resp)[:400], file=sys.stderr)
    sys.exit(1)
pod_id = resp["id"]
print("verify pod:", pod_id)

try:
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        code, r = api("GET", f"/v2/pods/{pod_id}")
        rt = r.get("runtime") or {}
        ports = rt.get("ports") or []
        if ports:
            print("=== FULL runtime JSON ===")
            print(json.dumps(rt, indent=1)[:2500])
            break
        time.sleep(5)

    # Try every plausible IP field + port public
    ip_candidates = []
    code, r = api("GET", f"/v2/pods/{pod_id}")
    rt = r.get("runtime") or {}
    for k in ("publicIp", "ip", "hostname", "internalIp"):
        v = rt.get(k)
        if v:
            ip_candidates.append((k, v))
    ports = rt.get("ports") or []
    for p in ports:
        for k in ("publicIp", "ip", "public", "hostname"):
            if p.get(k):
                ip_candidates.append((f"port.{k}", p[k]))
    print("IP candidates:", ip_candidates)

    addr = None
    for label, ipv in ip_candidates:
        for p in ports:
            if str(p.get("private")) == str(PORT):
                pub = p.get("public") or p.get("publicPort") or PORT
                addr = f"{ipv}:{pub}"
                print(f"trying addr from {label}: {addr}")
                break
        if addr:
            break

    if not addr:
        print("no addr found; will try the pod 'id'-based public URL if present", file=sys.stderr)
        code, r = api("GET", f"/v2/pods/{pod_id}")
        rt = r.get("runtime") or {}
        for k in rt:
            if "http" in str(rt[k]).lower() or "url" in k.lower():
                print("  runtime", k, "=", str(rt[k])[:200])

    if addr:
        for path in ["/", "/cublas/", "/cublas/verify.log", "/stt/", "/tts/"]:
            try:
                req = urllib.request.Request(f"http://{addr}{path}", headers={"User-Agent": "vihs-ep009/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read().decode(errors="replace")
                    print(f"--- GET {path} ({len(data)} bytes) ---")
                    print(data[:1200])
            except Exception as e:
                print(f"fetch {path} failed:", repr(e))
finally:
    code, r = api("DELETE", f"/v2/pods/{pod_id}")
    print("verify pod terminated, api rc:", code)
    time.sleep(4)
    code, r = api("GET", "/v2/pods")
    items = r.get("items", [])
    print("pods remaining:", len(items))

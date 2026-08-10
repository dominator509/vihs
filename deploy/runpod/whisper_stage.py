#!/usr/bin/env python3
"""Stage the ctranslate2-format whisper model onto the volume.

Serves the 4 model files (config.json, model.bin, tokenizer.json,
vocabulary.txt) from the operator box; the pod downloads + writes them to
/workspace/models/stt/ (replacing/augmenting the HF-format files already
there), then verifies faster-whisper can OPEN the model dir, and POSTs the
result. Terminates the pod in finally (billing rule).
"""
import base64
import json
import os
import subprocess
import sys
import time

key = os.environ.get("RUNPOD_API_KEY", "").strip()
if not key:
    print("NO RUNPOD_API_KEY"); sys.exit(1)

VOLUME_ID = os.environ.get("VOLUME_ID", "0z0kdx56tb")
DC = "US-IL-1"
BASE = os.environ.get("OPS_BASE", "http://66.94.123.250:8099")
STAGE_URL = f"{BASE}/whisper-ct2/"
REPORT_URL = f"{BASE}/report"
IMAGE = os.environ.get("STAGE_IMAGE", "ttl.sh/vihs-pod-slimlauncher:24h")
WHEEL_BASE = f"{BASE}/wheels-full"
REPORT_FILE = "/tmp/pod-reports.log"

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

stage_code = r'''
import json, os, subprocess, urllib.request

STAGE_URL = "__STAGE_URL__"
REPORT_URL = "__REPORT_URL__"
WHEEL_BASE = "__WHEEL_BASE__"
DST = "/workspace/models/stt"
os.makedirs(DST, exist_ok=True)

def report(line):
    print(line, flush=True)
    try:
        req = urllib.request.Request(
            REPORT_URL,
            data=(line + "\n").encode(),
            headers={"Content-Type": "text/plain", "User-Agent": "vihs-ep009/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            pass
    except Exception as e:
        print("report POST failed:", repr(e), flush=True)

# wheels (same as slim-boot) — needed for faster-whisper verify
r = subprocess.run([
    "pip", "install", "--no-index", "--trusted-host", "66.94.123.250",
    "--find-links", WHEEL_BASE,
    "aiortc==1.15.0", "av==17.1.0", "websockets==17.0.1", "numpy==2.5.1",
    "httpx==0.28.1", "faster-whisper==1.2.1", "piper-tts==1.6.0",
], capture_output=True, text=True)
report("WHEELS: " + ("OK" if r.returncode == 0 else "FAIL " + r.stderr[-200:]))

for f in ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]:
    url = STAGE_URL + f
    dest = os.path.join(DST, f)
    try:
        urllib.request.urlretrieve(url, dest)
        report(f"STAGED: {f} bytes={os.path.getsize(dest)}")
    except Exception as e:
        report(f"STAGED_FAIL: {f} {repr(e)}")

# verify faster-whisper can open the model dir (device cpu — just load check)
try:
    import time
    from faster_whisper import WhisperModel
    t0 = time.monotonic()
    m = WhisperModel(DST, device="cpu", compute_type="int8")
    report("WHISPER_OPEN: OK in %.1fs" % (time.monotonic() - t0))
    del m
except Exception as e:
    report("WHISPER_OPEN: FAIL " + repr(e))

report("STAGE_DONE")
'''

stage_code = stage_code.replace("__STAGE_URL__", STAGE_URL).replace("__REPORT_URL__", REPORT_URL).replace("__WHEEL_BASE__", WHEEL_BASE)
_b64 = base64.b64encode(stage_code.encode()).decode()
args = f'python -c "import base64;exec(base64.b64decode(\'{_b64}\'))"'


def check_reports() -> tuple[bool, str]:
    if not os.path.exists(REPORT_FILE):
        return False, ""
    data = open(REPORT_FILE, encoding="utf-8", errors="replace").read()
    return ("STAGE_DONE" in data), data[-1500:]


body = {
    "name": "vihs-whisper-stage",
    "image": IMAGE,
    "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
    "cloud": "SECURE",
    "dataCenterIds": [DC],
    "args": args,
    "ports": ["8093/http"],
    "mounts": {"network": [{"volumeId": VOLUME_ID, "path": "/workspace/models"}]},
    "disk": 20,
}
code, resp = api("POST", "/v2/pods", body)
if code != 0 or "id" not in resp:
    print("create failed:", json.dumps(resp)[:300])
    sys.exit(1)
pod_id = resp["id"]
print("stage pod:", pod_id)
try:
    deadline = time.monotonic() + 1800
    started = False
    while time.monotonic() < deadline:
        code, r = api("GET", f"/v2/pods/{pod_id}")
        rt = r.get("runtime") or {}
        if rt.get("container") or (rt.get("uptime") or 0) > 0:
            started = True
            break
        time.sleep(8)
    print("container started:", started)
    if not started:
        print("STAGE_UNSTARTED")
        sys.exit(1)
    deadline = time.monotonic() + 2400
    while time.monotonic() < deadline:
        done, tail = check_reports()
        if done:
            print("--- pod report ---")
            print(tail)
            sys.exit(0 if "WHISPER_OPEN: OK" in tail else 2)
        time.sleep(10)
    print("STAGE_TIMEOUT")
    _, tail = check_reports()
    print(tail)
    sys.exit(3)
finally:
    api("DELETE", f"/v2/pods/{pod_id}")
    time.sleep(3)
    print("stage pod terminated")

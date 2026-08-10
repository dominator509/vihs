#!/usr/bin/env python3
"""Conversation-turn probe: run the REAL Conversation._handle_turn on a pod.

Difference vs turn_probe.py: this exercises the exact conversation code
path (build_stages real=True + Conversation._handle_turn with real
MemoryClient -> memoryd, real metrics, real append_buffer) but bypasses
WebRTC by calling _handle_turn directly with an injected user queue item.
Progress markers POSTed back pinpoint the stall.

Pod self-terminates in finally. Billing rule: no held-open pods.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

key = os.environ.get("RUNPOD_API_KEY", "").strip()
if not key:
    print("NO RUNPOD_API_KEY")
    sys.exit(1)

VOLUME_ID = os.environ.get("VOLUME_ID", "0z0kdx56tb")
DC = "US-IL-1"
BASE = os.environ.get("OPS_BASE", "http://66.94.123.250:8099")
REPORT_URL = f"{BASE}/report"
IMAGE = os.environ.get("PROBE_IMAGE", "ttl.sh/vihs-pod-slimlauncher:24h")
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


probe_async = r'''
import asyncio, json, os, sys, time, urllib.request

REPORT_URL = "__REPORT_URL__"
WHEEL_BASE = "__WHEEL_BASE__"

def report(line):
    print(line, flush=True)
    try:
        req = urllib.request.Request(
            REPORT_URL, data=(line + "\n").encode(),
            headers={"Content-Type": "text/plain", "User-Agent": "vihs-ep009/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            pass
    except Exception as e:
        print("report POST failed:", repr(e), flush=True)

import subprocess as sp
r = sp.run([
    "pip", "install", "--no-index", "--trusted-host", "66.94.123.250",
    "--find-links", WHEEL_BASE,
    "aiortc==1.15.0", "av==17.1.0", "websockets==17.0.1", "numpy==2.5.1",
    "httpx==0.28.1", "faster-whisper==1.2.1", "piper-tts==1.6.0",
], capture_output=True, text=True)
report("CONV_WHEELS: " + ("OK" if r.returncode == 0 else "FAIL " + r.stderr[-300:]))

sys.path.insert(0, "/workspace/pod")

async def _main():
    import uuid
    from types import SimpleNamespace
    from vihs_pod.conversation import Conversation, build_stages
    from vihs_pod.memory_client import MemoryClient

    model_dir = os.environ.get("VIHS_MODEL_DIR", "/workspace/models")
    memoryd = os.environ.get("VIHS_MEMORYD_ADDR", "")
    pod_token = os.environ.get("VIHS_POD_TOKEN", "")
    report("CONV_START model_dir=" + model_dir + " memoryd=" + memoryd)

    # Build the REAL stages exactly like the agent does.
    stages, monitor = build_stages([], real=True)
    report("CONV_STAGES_BUILT")

    session_id = "conv-probe-" + uuid.uuid4().hex[:12]
    memory = MemoryClient("http://" + memoryd, pod_token=pod_token, timeout=5.0)

    # Create a session in memoryd so append/transcript work.
    try:
        async with __import__("httpx").AsyncClient(timeout=5.0) as c:
            r2 = await c.post(
                "http://" + memoryd + "/v1/sessions",
                json={"session_id": session_id, "persona_id": "e2e"},
                headers={"Authorization": "Bearer " + pod_token},
            )
            report("CONV_MKDIR_HTTP " + str(r2.status_code))
    except Exception as e:
        report("CONV_MKDIR_ERR " + repr(e))

    convo = Conversation(
        session_id=session_id,
        connection_id="conv-probe-conn",
        pod_token=pod_token,
        cursor={},
        memory=memory,
        stages=stages,
        monitor=monitor,
    )
    report("CONV_OBJECT_BUILT")

    # Bypass WebRTC: inject a user text directly and drive ONE turn.
    # Patch the response loop: just call _handle_turn once.
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(convo._handle_turn("First message before disconnect."), timeout=120.0)
        report("CONV_TURN_DONE %.1fs" % (time.monotonic() - t0))
    except asyncio.TimeoutError:
        report("CONV_TURN_HUNG >120s")
    except Exception as e:
        import traceback
        report("CONV_TURN_ERROR %r %s" % (e, traceback.format_exc()[-600:]))
    finally:
        try:
            await convo.stop()
        except Exception:
            pass

    # Check what actually landed in memoryd.
    try:
        async with __import__("httpx").AsyncClient(timeout=5.0) as c:
            r3 = await c.get(
                "http://" + memoryd + "/v1/sessions/" + session_id + "/transcript",
                headers={"Authorization": "Bearer " + pod_token},
            )
            report("CONV_TRANSCRIPT_HTTP " + str(r3.status_code))
            report("CONV_TRANSCRIPT " + r3.text[:400].replace("\n", "|"))
    except Exception as e:
        report("CONV_TRANSCRIPT_ERR " + repr(e))

    report("CONV_DONE")

asyncio.run(_main())
'''

probe_async = probe_async.replace("__REPORT_URL__", REPORT_URL).replace("__WHEEL_BASE__", WHEEL_BASE)
_b64 = base64.b64encode(probe_async.encode()).decode()
args = f'python -c "import base64;exec(base64.b64decode(\'{_b64}\'))"'


def check_reports() -> tuple[bool, str]:
    if not os.path.exists(REPORT_FILE):
        return False, ""
    data = open(REPORT_FILE, encoding="utf-8", errors="replace").read()
    return ("CONV_DONE" in data), data[-3000:]


body = {
    "name": "vihs-conv-probe",
    "image": IMAGE,
    "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
    "cloud": "SECURE",
    "dataCenterIds": [DC],
    "args": args,
    "ports": ["8093/http"],
    "mounts": {"network": [{"volumeId": VOLUME_ID, "path": "/workspace/models"}]},
    "disk": 20,
    "env": {
        "VIHS_LLM_URL": os.environ.get("VIHS_LLM_URL", ""),
        "VIHS_LLM_TOKEN": os.environ.get("VIHS_LLM_TOKEN", ""),
        "VIHS_LLM_PROVIDER": os.environ.get("VIHS_LLM_PROVIDER", ""),
        "VIHS_LLM_TLS_VERIFY": os.environ.get("VIHS_LLM_TLS_VERIFY", "1"),
        "VIHS_MODEL_DIR": "/workspace/models",
        "VIHS_MEMORYD_ADDR": os.environ.get("VIHS_MEMORYD_PUBLIC_ADDR", ""),
        "VIHS_POD_TOKEN": os.environ.get("VIHS_POD_TOKEN", ""),
        "LD_LIBRARY_PATH": "/workspace/models/cublas:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
    },
}
code, resp = api("POST", "/v2/pods", body)
if code != 0 or "id" not in resp:
    print("create failed:", json.dumps(resp)[:300])
    sys.exit(1)
pod_id = resp["id"]
print("conv probe pod:", pod_id)
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
        print("PROBE_UNSTARTED")
        sys.exit(1)
    deadline = time.monotonic() + 1500
    while time.monotonic() < deadline:
        done, tail = check_reports()
        if done:
            print("--- pod conv report ---")
            print(tail)
            ok = "CONV_TURN_DONE" in tail
            sys.exit(0 if ok else 2)
        time.sleep(10)
    print("PROBE_TIMEOUT; last reports:")
    _, tail = check_reports()
    print(tail)
    sys.exit(3)
finally:
    api("DELETE", f"/v2/pods/{pod_id}")
    time.sleep(3)
    print("conv probe pod terminated")

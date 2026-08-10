#!/usr/bin/env python3
"""Turn probe: run the REAL run_response path on a real RunPod pod.

Difference vs pipeline_probe.py: this exercises the actual conversation
turn (build_stages real=True + run_response) with per-stage timing and
progress markers POSTed back, so a hang pinpoints the exact stage.

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
from types import SimpleNamespace

REPORT_URL = "__REPORT_URL__"
WHEEL_BASE = "__WHEEL_BASE__"
results = []

def report(line):
    print(line, flush=True)
    results.append(line)
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
report("TURN_WHEELS: " + ("OK" if r.returncode == 0 else "FAIL " + r.stderr[-300:]))

sys.path.insert(0, "/workspace/pod")

async def _main():
    from vihs_pod.pipeline.abort_bus import AbortBus
    from vihs_pod.pipeline.flow import run_response
    from vihs_pod.pipeline.llm import AxiomGatewayLLM
    from vihs_pod.pipeline.tts import PiperTTS
    from vihs_pod.pipeline.mux import GStreamerMux
    from vihs_pod.pipeline.lipsync import StubLipSync

    model_dir = os.environ.get("VIHS_MODEL_DIR", "/workspace/models")
    llm = AxiomGatewayLLM(
        url=os.environ.get("VIHS_LLM_URL", ""),
        token=os.environ.get("VIHS_LLM_TOKEN", ""),
        provider=os.environ.get("VIHS_LLM_PROVIDER", "") or None,
        verify=os.environ.get("VIHS_LLM_TLS_VERIFY", "1") != "0",
        timeout=30.0,
    )
    tts = PiperTTS(
        binary=os.environ.get("VIHS_TTS_BIN", "piper"),
        voice=os.path.join(model_dir, "tts", "en_US-lessac-medium.onnx"),
    )
    st = SimpleNamespace(llm=llm, tts=tts, lipsync=StubLipSync(), mux=GStreamerMux())
    bus = AbortBus()
    ledger = {}
    gen = bus.fresh()

    report("TURN_START model_dir=" + model_dir)
    report("TURN_TTS_VOICE=" + tts.voice)

    orig_llm_stream = llm.stream
    orig_tts_stream = tts.stream

    async def traced_llm(prompt):
        t0 = time.monotonic()
        report("LLM_BEGIN")
        n = 0
        async for c in orig_llm_stream(prompt):
            n += 1
            if n == 1:
                report("LLM_FIRST_DELTA %.1fs" % (time.monotonic() - t0))
            yield c
        report("LLM_END deltas=%d %.1fs" % (n, time.monotonic() - t0))

    async def traced_tts(clause, voice):
        t0 = time.monotonic()
        report("TTS_BEGIN clause=%r" % clause[:30])
        n = 0
        async for ch in orig_tts_stream(clause, voice):
            n += 1
            if n == 1:
                report("TTS_FIRST_CHUNK %.1fs" % (time.monotonic() - t0))
            yield ch
        report("TTS_END chunks=%d %.1fs" % (n, time.monotonic() - t0))

    llm.stream = traced_llm
    tts.stream = traced_tts

    t0 = time.monotonic()
    try:
        committed = await asyncio.wait_for(
            run_response(gen, bus, st,
                         "user: First message before disconnect.\n",
                         1, ledger,
                         on_caption=lambda tid, d: asyncio.sleep(0)),
            timeout=90.0,
        )
        report("TURN_COMMITTED %.1fs text=%r" % (time.monotonic() - t0, committed.text[:80]))
    except asyncio.TimeoutError:
        report("TURN_HUNG >90s")
    except Exception as e:
        import traceback
        report("TURN_ERROR %r %s" % (e, traceback.format_exc()[-500:]))
    finally:
        try:
            await tts.close()
        except Exception:
            pass
    report("TURN_DONE")

asyncio.run(_main())
'''

probe_async = probe_async.replace("__REPORT_URL__", REPORT_URL).replace("__WHEEL_BASE__", WHEEL_BASE)
_b64 = base64.b64encode(probe_async.encode()).decode()
args = f'python -c "import base64;exec(base64.b64decode(\'{_b64}\'))"'


def check_reports() -> tuple[bool, str]:
    if not os.path.exists(REPORT_FILE):
        return False, ""
    data = open(REPORT_FILE, encoding="utf-8", errors="replace").read()
    return ("TURN_DONE" in data), data[-3000:]


body = {
    "name": "vihs-turn-probe",
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
        "LD_LIBRARY_PATH": "/workspace/models/cublas:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
    },
}
code, resp = api("POST", "/v2/pods", body)
if code != 0 or "id" not in resp:
    print("create failed:", json.dumps(resp)[:300])
    sys.exit(1)
pod_id = resp["id"]
print("turn probe pod:", pod_id)
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
            print("--- pod turn report ---")
            print(tail)
            ok = "TURN_COMMITTED" in tail
            sys.exit(0 if ok else 2)
        time.sleep(10)
    print("PROBE_TIMEOUT; last reports:")
    _, tail = check_reports()
    print(tail)
    sys.exit(3)
finally:
    api("DELETE", f"/v2/pods/{pod_id}")
    time.sleep(3)
    print("turn probe pod terminated")

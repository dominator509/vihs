#!/bin/sh
# VIHS pod slim bootstrap — install the llama-cpp-python wheel (not baked;
# 1.36GB) and prep local models, then exec the real agent. Baked into the
# slim launcher image.
#
# EP-010 M2 (bake): the base runtime wheel closure (56 wheels, ~180MB) is
# installed at BUILD time (see pod-slim.Dockerfile) and marked with
# /opt/vihs-wheels-installed, so a cold boot no longer pip-installs from the
# operator box — that step is where bad nodes stalled (I-02). Only the
# 1.36GB llama-cpp-python wheel installs here, and only when VIHS_LLAMA_GGUF
# is set; the GGUF (4.9GB) and Piper voice (63MB) stay on the network volume
# and are copied/downloaded locally once.
#
# The wheel mirror lives on the operator box (66.94.123.250:8099) — the
# same host pods are proven to reach at speed (581MB cublas wheel). The
# mirror holds the FULL dependency closure, so installs run with --no-index
# and need no PyPI access.
#
# EP-009 M4 diagnosis: when VIHS_LOG_POST is set, every agent stdout line
# is POSTed to that URL (the operator report server) so pod-side failures
# are observable without RunPod console access.
set -eu

WHEEL_BASE="${VIHS_WHEEL_BASE:-http://66.94.123.250:8099/wheels-full}"
MARKER=/opt/vihs-wheels-installed

if [ ! -f "$MARKER" ]; then
    echo "slim-boot: installing wheels from $WHEEL_BASE"
    # --no-index: resolve strictly from the mirror; --find-links points at it.
    # --trusted-host: the mirror is plain HTTP on the operator box (no TLS).
    # pip needs a writable cache dir; PIP_NO_CACHE_DIR=1 is set in the image.
    pip install --no-index --trusted-host 66.94.123.250 --find-links "$WHEEL_BASE" \
        aiortc==1.15.0 \
        av==17.1.0 \
        websockets==17.0.1 \
        numpy==2.5.1 \
        httpx==0.28.1 \
        faster-whisper==1.2.1 \
        piper-tts==1.6.0
    touch "$MARKER"
    echo "slim-boot: wheels installed"
fi

echo "slim-boot: starting agent"
# TTS fast path (EP-010 M2): copy the Piper voice model from the network
# volume to LOCAL disk at boot. The volume read is ~20MB/s (~3.5s for the
# 63MB ONNX) and piper reloads the model on every process spawn (per
# session) — that single reload blew the 300ms tts_ttfa budget every
# turn. One local copy at boot amortizes it: subsequent spawns load from
# NVMe + page cache (~150-300ms). Keep the volume copy untouched.
# NOTE: run regardless of VIHS_TTS_VOICE being set (the staging driver
# pins it to the volume path) — we OVERRIDE it to the local copy so the
# agent's in-process warmup never reads the network volume.
if [ -n "${VIHS_MODEL_DIR:-}" ] && [ -d "$VIHS_MODEL_DIR/tts" ]; then
    TTS_LOCAL=/opt/vihs-tts
    mkdir -p "$TTS_LOCAL"
    _voice="$(ls "$VIHS_MODEL_DIR"/tts/*.onnx 2>/dev/null | head -1 || true)"
    if [ -n "$_voice" ]; then
        _base="$(basename "$_voice")"
        if [ ! -f "$TTS_LOCAL/$_base" ]; then
            echo "slim-boot: copying TTS voice to local disk ($_base)"
            cp "$_voice" "$TTS_LOCAL/$_base"
        fi
        # Copy the JSON config too — PiperVoice.load REQUIRES it
        # ({model}.json); without it the in-process warmup fails silently.
        if [ -f "$VIHS_MODEL_DIR/tts/$_base.json" ] && [ ! -f "$TTS_LOCAL/$_base.json" ]; then
            echo "slim-boot: copying TTS voice config to local disk"
            cp "$VIHS_MODEL_DIR/tts/$_base.json" "$TTS_LOCAL/$_base.json"
        fi
        export VIHS_TTS_VOICE="$TTS_LOCAL/$_base"
        echo "slim-boot: VIHS_TTS_VOICE=$VIHS_TTS_VOICE"
        # Boot-time warm: one dummy clause forces ONNX init so the page
        # cache holds the model; the first real session then skips the
        # cold read entirely.
        if command -v piper >/dev/null 2>&1; then
            echo "slim-boot: warming piper voice model"
            echo "Warm." | piper --model "$VIHS_TTS_VOICE" --output-raw \
                >/dev/null 2>&1 || true
        fi
    fi
fi

# Local LLM bootstrap (EP-010 M2 option c): when VIHS_LLAMA_GGUF points at a
# GGUF on the model volume, install llama-cpp-python from the wheel mirror,
# download the model if missing, and start the OpenAI-compat server on
# 127.0.0.1:8000 (the pod's PROVIDER=vllm adapter targets that URL by
# default). GPU offload all layers (-1); 4096 ctx; prompt cache on (VIHS
# sends the same preamble every turn, so repeated prefill is skipped).
if [ -n "${VIHS_LLAMA_GGUF:-}" ]; then
    echo "slim-boot: local LLM enabled (GGUF: $VIHS_LLAMA_GGUF)"
    # llama-cpp-python is NOT baked (1.36GB wheel; would balloon the image).
    # Retry the install like the GGUF download: a bad node's egress can
    # stall mid-fetch (I-02 pattern), and one retry is cheaper than a
    # crash-looping boot.
    _tries=0
    while [ "$_tries" -lt 10 ]; do
        _tries=$((_tries + 1))
        if pip install --no-index --trusted-host 66.94.123.250 --find-links "$WHEEL_BASE" \
            "llama-cpp-python[server]==0.3.19"; then
            break
        fi
        echo "slim-boot: llama wheel install failed (try $_tries) — retrying"
        sleep 3
    done
    if [ ! -f "$VIHS_LLAMA_GGUF" ]; then
        mkdir -p "$(dirname "$VIHS_LLAMA_GGUF")"
        echo "slim-boot: downloading GGUF from ${VIHS_LLAMA_GGUF_URL:-<unset>}"
        # Resumable: -C - continues a partial download; the operator mirror
        # serves Range (206). Retry until curl exits 0 (EP-010 M2: a dropped
        # connection must not kill the whole boot). Expected size optional.
        _part="${VIHS_LLAMA_GGUF}.part"
        _want="${VIHS_LLAMA_GGUF_SIZE:-0}"
        _tries=0
        while [ "$_tries" -lt 20 ]; do
            _tries=$((_tries + 1))
            if curl -sL -C - --max-time 900 -o "$_part" "${VIHS_LLAMA_GGUF_URL}"; then
                _got=$(wc -c < "$_part" 2>/dev/null || echo 0)
                if [ "$_want" -gt 0 ] && [ "$_got" != "$_want" ]; then
                    echo "slim-boot: GGUF size mismatch got=$_got want=$_want (retry)"
                    rm -f "$_part"
                    continue
                fi
                echo "slim-boot: GGUF download complete ($_got bytes, try $_tries)"
                mv "$_part" "$VIHS_LLAMA_GGUF"
                break
            fi
            echo "slim-boot: GGUF download interrupted (try $_tries) — resuming"
            sleep 3
        done
        if [ ! -f "$VIHS_LLAMA_GGUF" ]; then
            echo "slim-boot: GGUF download failed after $_tries tries" >&2
            exit 1
        fi
    fi
    python -m llama_cpp.server \
        --model "$VIHS_LLAMA_GGUF" \
        --model_alias default \
        --host 127.0.0.1 --port 8000 \
        --n_gpu_layers -1 \
        --n_ctx 4096 \
        --cache true > /tmp/llama-server.log 2>&1 &
    LLAMA_PID=$!
    i=0
    until curl -s --max-time 2 http://127.0.0.1:8000/v1/models >/dev/null 2>&1; do
        i=$((i+1))
        if [ "$i" -gt 300 ]; then
            echo "slim-boot: llama-server failed to start; log:" >&2
            tail -50 /tmp/llama-server.log >&2
            exit 1
        fi
        sleep 1
    done
    echo "slim-boot: llama-server ready (pid $LLAMA_PID)"
fi

if [ -n "${VIHS_LOG_POST:-}" ]; then
    # Tee agent stdout into a log-forwarder that POSTs each line to the
    # operator report server. The agent's own prints stay on stdout too
    # (RunPod console), so nothing is lost.
    exec python -m vihs_pod.agent 2>&1 | while IFS= read -r line; do
        printf '%s\n' "$line"
        curl -s --max-time 5 -X POST -H "Content-Type: text/plain" \
            -H "User-Agent: vihs-ep009/1.0" \
            --data-binary "PODLOG $line" "$VIHS_LOG_POST" >/dev/null 2>&1 || true
    done
else
    exec python -m vihs_pod.agent
fi

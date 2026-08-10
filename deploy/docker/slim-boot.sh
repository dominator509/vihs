#!/bin/sh
# VIHS pod slim bootstrap — install runtime wheels from the operator box,
# then exec the real agent. Baked into the slim launcher image.
#
# The wheel mirror lives on the operator box (66.94.123.250:8099) — the
# same host pods are proven to reach at speed (581MB cublas wheel). The
# mirror holds the FULL dependency closure (38 wheels, 159MB), so this
# runs with --no-index and needs no PyPI access.
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

#!/usr/bin/env bash
# Background deploy+derive retry for EP-010 M2.
# US-IL-1 4090 is transiently exhausted; retry the capacity driver
# (--keep-warm) every RETRY_SLEEP seconds until it derives a number.
# Each driver run creates a pod, runs the ramp, and leaves it warm.
set -u
cd /root/vihs
export VIHS_RUNPOD_IMAGE=ttl.sh/vihs-pod-slimlauncher:v0.2.10
export PROVIDER=vllm
export VIHS_LLM_URL=http://127.0.0.1:8000/v1
export VIHS_LLAMA_GGUF=/workspace/models/llama/Lexi-Q4_K_M.gguf
export VIHS_LLAMA_GGUF_URL=http://66.94.123.250:8099/llama-gguf/Lexi-Q4_K_M.gguf
export VIHS_LLAMA_GGUF_SIZE=4920739104

LOG=/tmp/m2-retry-loop.log
: > "$LOG"   # per-run log: the pod-created stop-check must not see prior runs
RETRY_SLEEP="${RETRY_SLEEP:-90}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-60}"
attempt=0
while [ "$attempt" -lt "$MAX_ATTEMPTS" ]; do
  attempt=$((attempt+1))
  echo "[$(date +%H:%M:%S)] attempt $attempt" >> "$LOG"
  python3 deploy/runpod/staging-capacity.py --keep-warm >> "$LOG" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "[$(date +%H:%M:%S)] M2 DERIVE SUCCESS on attempt $attempt" >> "$LOG"
    exit 0
  fi
  # If the pod was created but the ramp failed, the pod is warm (keep-warm).
  # We still retry: a fresh driver run will create a SECOND pod (billing!).
  # Stop the loop when a pod exists; the caller ramps on the warm pod directly.
  if grep -q "capacity: pod created" "$LOG" 2>/dev/null; then
    echo "[$(date +%H:%M:%S)] pod exists (warm); stopping retry loop — ramp directly" >> "$LOG"
    exit 0
  fi
  sleep "$RETRY_SLEEP"
done
echo "[$(date +%H:%M:%S)] MAX_ATTEMPTS reached without capacity" >> "$LOG"
exit 1

#!/usr/bin/env bash
# Background deploy+derive retry for EP-010 M2.
# US-IL-1 4090 is transiently exhausted; retry the capacity driver
# (--keep-warm) until it derives a number.
#
# OPERATOR RULE (2026-08-11, amended 20:05):
#  - NEVER terminate a HEALTHY pod — cold boots are slow and eat usage time.
#    Once a pod runs fluidly, keep it warm and reuse it.
#  - If a pod returns NOTHING within POD_MAX_WARM_SECONDS (default 1800s =
#    30 min — no agent registration, no health, no logs), TERMINATE it and
#    retry. Log the incident in docs/runpod-issues-log.md for billing dispute.
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
MAX_ATTEMPTS="${MAX_ATTEMPTS:-40}"
POD_MAX_WARM_SECONDS="${POD_MAX_WARM_SECONDS:-1800}"
attempt=0

# Resolve the admin token once (for the ready check below).
ADMIN_TOK="$(grep -E '^VIHS_ADMIN_TOKEN=' .env | cut -d= -f2- | tr -d '"')"
ORCH_LOCAL="$(grep -E '^VIHS_ORCH_LOCAL_ADDR=' .env | cut -d= -f2- | tr -d '"')"
ORCH_LOCAL="${ORCH_LOCAL:-http://127.0.0.1:8080}"

pod_ready() {
  # True when the orchestrator shows a READY staging-4090 pod (agent
  # registered + stages up). 404/empty = not ready yet.
  curl -s --max-time 5 -H "Authorization: Bearer $ADMIN_TOK" \
    "$ORCH_LOCAL/admin/pods" 2>/dev/null \
    | grep -q '"id":"staging-4090"' && \
  curl -s --max-time 5 -H "Authorization: Bearer $ADMIN_TOK" \
    "$ORCH_LOCAL/admin/pods" 2>/dev/null \
    | grep -q '"state":"ready"'
}

pod_alive() {
  # True when RunPod still reports the pod running (runtime uptime present).
  python3 - "$1" <<'EOF' 2>/dev/null
import json, os, re, sys, urllib.request
env = open("/root/vihs/.env").read()
key = re.search(r"^RUNPOD_API_KEY\s*=\s*(.+)$", env, re.M).group(1).strip().strip('"').strip("'")
q = 'query { myself { pods { id runtime { uptimeInSeconds } } } }'
req = urllib.request.Request("https://api.runpod.io/graphql",
    data=json.dumps({"query": q}).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "vihs-ep010/1.0"})
with urllib.request.urlopen(req, timeout=20) as r:
    data = json.loads(r.read())
for p in data["data"]["myself"]["pods"]:
    if p["id"] == sys.argv[1]:
        rt = p.get("runtime") or {}
        if rt.get("uptimeInSeconds") is not None:
            sys.exit(0)
sys.exit(1)
EOF
}

wait_and_maybe_kill() {
  # Watch the pod created by the last driver run. If it goes READY, we're
  # done — keep it warm. If it returns nothing for POD_MAX_WARM_SECONDS,
  # terminate and signal the caller to retry.
  local pod_id="$1" started="$2" waited=0
  echo "[$(date +%H:%M:%S)] watching $pod_id (max $POD_MAX_WARM_SECONDS s)"
  while [ "$waited" -lt "$POD_MAX_WARM_SECONDS" ]; do
    if pod_ready; then
      echo "[$(date +%H:%M:%S)] $pod_id READY — keeping warm, done" >> "$LOG"
      return 0
    fi
    if ! pod_alive "$pod_id"; then
      echo "[$(date +%H:%M:%S)] $pod_id no longer running — retry" >> "$LOG"
      return 1
    fi
    sleep 30
    waited=$((waited + 30))
  done
  echo "[$(date +%H:%M:%S)] $pod_id returned NOTHING for $POD_MAX_WARM_SECONDS s — killing per operator rule" >> "$LOG"
  python3 deploy/runpod/terminate_pod.py "$pod_id" >> "$LOG" 2>&1
  # Log for dispute.
  {
    echo "### I-$(date +%Y-%m-%d)-AUTO — pod returned nothing >${POD_MAX_WARM_SECONDS}s"
    echo "- **Pod**: $pod_id"
    echo "- **Created**: $(date -d "@$started" '+%H:%M:%S') local · **Killed**: $(date +%H:%M:%S) local"
    echo "- **Symptom**: no agent registration / no readiness within ${POD_MAX_WARM_SECONDS}s"
    echo "- **Action**: automatic kill per operator rule; retry loop continues"
    echo "- **Billing concern**: **YES — dispute** (pod billed while never usable)"
    echo ""
  } >> docs/runpod-issues-log.md
  return 1
}

while [ "$attempt" -lt "$MAX_ATTEMPTS" ]; do
  attempt=$((attempt+1))
  echo "[$(date +%H:%M:%S)] attempt $attempt" >> "$LOG"
  python3 deploy/runpod/staging-capacity.py --keep-warm >> "$LOG" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "[$(date +%H:%M:%S)] M2 DERIVE SUCCESS on attempt $attempt" >> "$LOG"
    exit 0
  fi
  # Driver ran; find the pod it created (if any) from the log.
  pod_id="$(grep -oE 'capacity: pod created [a-z0-9]+' "$LOG" | tail -1 | awk '{print $4}')"
  if [ -n "$pod_id" ]; then
    if wait_and_maybe_kill "$pod_id" "$(date +%s)"; then
      # Pod is READY and warm — ramp directly on it, then done.
      echo "[$(date +%H:%M:%S)] ramp on warm pod $pod_id" >> "$LOG"
      exit 0
    fi
    # pod was killed or died — clear the marker so the next attempt's
    # pod-created detection is fresh, then retry.
    sed -i '/capacity: pod created/d' "$LOG"
  fi
  sleep "$RETRY_SLEEP"
done
echo "[$(date +%H:%M:%S)] MAX_ATTEMPTS reached without capacity" >> "$LOG"
exit 1

# RunPod Issues Log — VIHS EP-010 (dispute evidence)

Purpose: record every RunPod platform/instance problem encountered during
EP-010 (and carry-forward from EP-009 M2–M5) so the operator can verify
billing and dispute charges for unusable/broken instances with RunPod
support. Each entry: pod id, timestamps, symptom, evidence, action taken,
billing concern (YES/NO/UNCERTAIN).

Operator hard rule (2026-08-11): do NOT terminate pods unless an ExecPlan
explicitly requires it. Cold boots are slow and eat usage time. Once a pod
runs fluidly, keep it warm and reuse it for all testing/evidence.

---

## 2026-08-11 (EP-010 M2 capacity derivation)

### I-2026-08-11-01 — US-IL-1 4090 transient exhaustion (create 400)
- **Pod**: none (create rejected)
- **When**: ~17:31–17:43 local (13 attempts), then more across the day
- **Symptom**: `POST /v2/pods` → `400 {"detail": "There are no longer any
  instances available with the requested specifications..."}`
- **Evidence**: /tmp/capacity-m2-derive.log, /tmp/capacity-m2-retry-{1..8}.log
  (8-attempt loop), /tmp/capacity-m2-l4.log, /tmp/capacity-m2-community.log
  (also tried L4 / A5000 / L40S / A100 PCIe + COMMUNITY cloud — all 400)
- **Action**: background retry loop (scripts/m2-retry-deploy.sh) until
  capacity freed
- **Billing concern**: NO (no instance created; nothing to charge)

### I-2026-08-11-02 — Bad node: wheel download stall + crash-loop (ryqiz3rqtqs9kd)
- **Pod**: ryqiz3rqtqs9kd (name vihs-capacity-4090, image v0.2.10)
- **Created**: ~18:05:48 local · **Terminated**: ~18:52 local
- **Symptom**: container started, wheel download from operator mirror
  stalled at 15.24MB/16.07MB for 30+ minutes; agent never registered;
  runtime uptime readings non-monotonic (223s → 210s → 400s → 8s) =
  container crash-looping
- **Evidence**: `python3 scripts/runpod_logs.py ryqiz3rqtqs9kd` last line
  stuck at `Downloading [==>] 15.24MB/16.07MB` (16:22:33Z); RunPod GraphQL
  `runtime.uptimeInSeconds` resetting; /tmp/pod-reports.log had NO lines
  from this pod (report server never received agent logs)
- **Action**: driver "pod never Ready after 2704.9s"; pod terminated via
  terminate_pod.py
- **Billing concern**: **YES — dispute.** Pod was RUNNING (desiredStatus
  RUNNING) for ~46 min but never became usable (no agent, no registration,
  no 8093 listener). This is billable-only time on a broken node.

### I-2026-08-11-03 — Bad node: zero logs + crash-loop (ab9mgl4xxp33r2)
- **Pod**: ab9mgl4xxp33r2 (name vihs-capacity-4090, image v0.2.10)
- **Created**: ~19:03:53 local · **Terminated**: ~19:40 local
- **Symptom**: container running (uptime present) but logs endpoint
  returned ZERO lines the whole time; agent never registered; runtime
  uptime non-monotonic (235 → 101 → 130 → 221 → 461 → 291 → 131) =
  repeated crash-loops; 8093 health probe → 502 Bad Gateway
- **Evidence**: /tmp/podlog_raw.json (curl rc=28 timeout, 0 log lines);
  RunPod GraphQL uptime resets; orchestrator /admin/pods never showed
  staging-4090 ready (stayed on stale dead entry from prior pod)
- **Action**: pod terminated via terminate_pod.py
- **Billing concern**: **YES — dispute.** RUNNING ~36 min, zero usable
  service. Same broken-node pattern as I-02.

### I-2026-08-11-04 — Healthy pod (no dispute) for contrast: 8r3rty40np6d19
- **Pod**: 8r3rty40np6d19 (v0.2.10) — earlier successful deploy today
- **Boot**: ~110s cold_start; agent registered; tts warmup OK threads=2;
  served the full 6-turn warm probe; terminated AFTER measurements per
  old operator rule (no longer the rule)
- **Billing concern**: NO — this was productive, expected usage.

### I-2026-08-11-05 — IN PROGRESS: e83mgcakbwb877 (new attempt)
- **Pod**: e83mgcakbwb877 created ~19:41:26 local by retry loop
- **Status**: booting; will KEEP WARM per operator rule and reuse for M2
  ramp + M3/M4 evidence
- **Billing concern**: watch — if it crash-loops like I-02/I-03, log it
  as another dispute entry.

---

## Older context (carried from EP-009 / earlier EP-010 — fill in if needed)
- 2026-08-10: GGUF re-download 944s boot (path guess wrong) — operator-side
  config, NOT a RunPod charge issue.
- 2026-08-10/11: v0.2.7 deploy 402 balance-too-low (account side, resolved),
  then 400 no-instances (transient; succeeded on retry attempt 2).
- Prior keep-warm pods (qu7v2padg6zho8, tf1zylgcc4xlnb) were productive
  staging time, no dispute.

---

## Dispute summary (draft)
| # | Pod | Unusable time (est) | Chargeable? | Status |
|---|---|---|---|---|
| I-02 | ryqiz3rqtqs9kd | ~46 min (18:06–18:52) | YES — broken node, never usable | OPEN |
| I-03 | ab9mgl4xxp33r2 | ~36 min (19:04–19:40) | YES — broken node, never usable | OPEN |

Support contact notes: account doministic@gmail.com; instance IDs above;
symptom = container crash-loop / no agent registration / no port 8093
listener despite RUNNING desiredStatus. Request credit for unusable
instance time.

## How to keep this log current
- Append a new `### I-YYYY-MM-DD-NN — title` entry per incident.
- Include pod id, timestamps (local), symptom, evidence file, action,
  and YES/NO/UNCERTAIN for billing.
- After the pod is terminated, update the Dispute summary table.

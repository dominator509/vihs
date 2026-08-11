# RunPod Issues Log — VIHS EP-010 (dispute evidence)

Purpose: record every RunPod platform/instance problem encountered during
EP-010 (and carry-forward from EP-009 M2–M5) so the operator can verify
billing and dispute charges for unusable/broken instances with RunPod
support. Each entry: pod id, timestamps, symptom, evidence (with links),
action taken, billing concern (YES/NO/UNCERTAIN).

Evidence artifacts live in `docs/runpod-evidence/` (committed). Raw console
dumps are also on this host under /tmp with the file names listed.

Operator rules (2026-08-11):
1. Do NOT terminate a HEALTHY pod — cold boots are slow and eat usage time.
   Once a pod runs fluidly, keep it warm and reuse it for all evidence.
2. Kill-and-retry a pod that returns NOTHING for 30 minutes (no agent
   registration, no health, no logs) — enforced by scripts/m2-retry-deploy.sh.

---

## 2026-08-11 (EP-010 M2 capacity derivation)

### I-2026-08-11-01 — US-IL-1 4090 transient exhaustion (create 400)
- **Pod**: none (create rejected)
- **When**: ~17:31–17:43 local (13 attempts), then more across the day
- **Symptom**: `POST /v2/pods` → `400 {"detail": "There are no longer any
  instances available with the requested specifications..."}`
- **Evidence**:
  - docs/runpod-evidence/capacity-m2-derive.log
  - docs/runpod-evidence/capacity-m2-retry-{1..8}.log (8-attempt loop)
  - Also tried L4 / A5000 / L40S / A100 PCIe + COMMUNITY cloud — all 400
    (see /tmp/capacity-m2-l4.log, /tmp/capacity-m2-community.log)
- **Action**: background retry loop (scripts/m2-retry-deploy.sh) until
  capacity freed
- **Billing concern**: NO (no instance created; nothing to charge)

### I-2026-08-11-02 — Bad node: wheel download stall + crash-loop (ryqiz3rqtqs9kd)
- **Pod**: ryqiz3rqtqs9kd (name vihs-capacity-4090, image v0.2.10)
- **Created**: ~18:05:48 local · **Terminated**: ~18:52 local (~46 min billed)
- **Symptom**: container started and pulled the image, then the wheel
  install from OUR operator mirror stalled; agent never registered; runtime
  uptime non-monotonic (223s → 210s → 400s → 8s) = container crash-looping
- **Evidence**:
  - docs/runpod-evidence/pod-ryqiz3rqtqs9kd-logs-raw.json — raw RunPod
    console dump (SSE): image layer pull at 16:22Z then a **39-min gap**
    before pip wheel fetches at 17:01Z, stalling at `pylibsrtp` from
    http://66.94.123.250:8099/wheels-full/ — node egress to our mirror
    stalled mid-install
  - docs/runpod-evidence/pod-ryqiz3rqtqs9kd-stall-extract.txt — first/last
    console lines showing the stall
  - RunPod GraphQL `runtime.uptimeInSeconds` resetting (crash-loop)
  - /tmp/pod-reports.log had NO lines from this pod (report server never
    received agent logs)
- **Action**: driver "pod never Ready after 2704.9s"; pod terminated via
  terminate_pod.py
- **Billing concern**: **YES — dispute.** RUNNING ~46 min, never usable (no
  agent, no registration, no 8093 listener).

### I-2026-08-11-03 — Bad node: zero logs + crash-loop (ab9mgl4xxp33r2)
- **Pod**: ab9mgl4xxp33r2 (name vihs-capacity-4090, image v0.2.10)
- **Created**: ~19:03:53 local · **Terminated**: ~19:40 local (~36 min billed)
- **Symptom**: container running (uptime present) but logs endpoint returned
  ZERO lines the whole time; agent never registered; runtime uptime
  non-monotonic (235 → 101 → 130 → 221 → 461 → 291 → 131) = repeated
  crash-loops; 8093 health probe → 502 Bad Gateway
- **Evidence**:
  - docs/runpod-evidence/pod-ab9mgl4xxp33r2-logs.json — empty logs payload
  - docs/runpod-evidence/ready-wait2.log — orchestrator /admin/pods poll,
    staging-4090 never left stale-dead state
  - RunPod GraphQL uptime resets (captured in session)
- **Action**: pod terminated via terminate_pod.py
- **Billing concern**: **YES — dispute.** RUNNING ~36 min, zero usable
  service. Same broken-node pattern as I-02.

### I-2026-08-11-04 — Healthy pod (no dispute) for contrast: 8r3rty40np6d19
- **Pod**: 8r3rty40np6d19 (v0.2.10) — earlier successful deploy today
- **Boot**: ~110s cold_start; agent registered; tts warmup OK threads=2;
  served the full 6-turn warm probe (tts_ttfa p50 68–152ms, llm_ttft
  26–185ms)
- **Billing concern**: NO — productive, expected usage. Contrast case:
  SAME image, SAME DC, healthy node boots in ~2 min.

### I-2026-08-11-05 — Bad node, killed per 30-min rule (e83mgcakbwb877)
- **Pod**: e83mgcakbwb877 · created 19:41:26 local · killed ~20:08 local
  (~27 min billed)
- **Symptom**: returned NOTHING — zero console logs, no agent registration,
  8093 health 502. One crash-restart at 673s uptime; cycle 2 ran to 800s+
  still silent. Same broken-node signature as I-02/I-03.
- **Evidence**:
  - docs/runpod-evidence/pod-e83mgcakbwb877-uptime-monitor.log — 30s
    uptime-delta monitor showing the 673→19 crash-restart then steady
    climbing with zero readiness
  - 502 on https://e83mgcakbwb877-8093.proxy.runpod.net/health
  - empty logs endpoint (RunPod console)
- **Action**: terminated per operator 30-min rule
- **Billing concern**: **YES — dispute.** Pod billed ~27 min, never usable.

### I-2026-08-11-06 — IN PROGRESS: 4lby9klwrazkav (current attempt)
- **Pod**: 4lby9klwrazkav created ~20:07:35 local by enforcement loop
- **Status**: under 30-min watch; will keep warm if it becomes usable
- **Billing concern**: watch — log if it returns nothing.

---

## Dispute summary (draft)

| # | Pod | Unusable time (est) | Chargeable? | Status |
|---|---|---|---|---|
| I-02 | ryqiz3rqtqs9kd | ~46 min (18:06–18:52) | YES — broken node, never usable | OPEN |
| I-03 | ab9mgl4xxp33r2 | ~36 min (19:04–19:40) | YES — broken node, never usable | OPEN |
| I-05 | e83mgcakbwb877 | ~27 min (19:41–20:08) | YES — broken node, never usable | OPEN |

Total disputed to date: ~1h49m of billed-but-unusable instance time.

Support contact notes: account doministic@gmail.com; instance IDs above;
symptom = container crash-loop / no agent registration / no port 8093
listener despite RUNNING desiredStatus. Request credit for unusable
instance time. Reference RunPod's own ">7 min = unhealthy" init guidance
(see Research below) — our pods hung 3–6× that long.

---

## Research: is this common / are we doing something wrong? (2026-08-11)

### Community reports (same symptom as ours)
- **AnswerOverflow/Discord (Dec 2024)**: "Throttled download speed from
  container registry while still being billed" — user's image pull took
  ~45 min, billed while waiting; called it a "conflict of interests".
  URL: https://www.answeroverflow.com/m/1319757071448543346
- **AnswerOverflow/Discord (Jul 2025)**: "Registry fetching extremely slow
  for the past 2 days" — a 130GB image that used to boot in ~20 min was
  taking 60+ min; slowdown coincided with a RunPod change.
  URL: https://www.answeroverflow.com/m/1400148062248239104
- **Reddit r/RunPod (Mar 2026)**: "Is anyone having to stop and start pods
  over and over to get them running correctly?" — frequent hung/reset pods.
  URL: https://www.reddit.com/r/RunPod/comments/1s2be4u/
- **RunPod's own worker-vllm repo, Issue #111 (Sep 2024)**: "Very slow cold
  starts even with flashboot" — RunPod staff acknowledged the flow change
  and pointed at model caching / baking models into the image.
  URL: https://github.com/runpod-workers/worker-vllm/issues/111
- **Pierce Freeman, "Speeding up RunPod" (Dec 2023)**: documents "varying
  runtime performance box-to-box" on RunPod — matches our good-node/bad-node
  split with the same image.
  URL: https://pierce.dev/notes/speeding-up-runpod

### Official RunPod documentation
- **Billing** (https://docs.runpod.io/accounts-billing/billing): "All
  compute and storage charges are billed per second"; credits deducted in
  real-time based on active Pods. **No grace period for initialization or
  image pull** — you are billed from pod creation while the image
  downloads, even if the pod never becomes usable.
- **Optimization guide**
  (https://docs.runpod.io/serverless/development/optimization):
  - Initialization time = "Downloading Docker image"; cold start = model
    load into GPU.
  - "If cold start exceeds 7 minutes, the worker is marked unhealthy."
    Our bad pods hang 30+ min — 3–6× their own unhealthy threshold.
  - Recommended fixes: cached models; **bake models into the image** (load
    from local NVMe instead of downloading at runtime); active workers > 0;
    multiple GPU types for availability.
  - Network volumes: "restrict to specific data centers" (matches our
    US-IL-1 pin — we cannot spread regions while the volume is there).

---

## Are we baking models into images? — no, and why

**Current state: NO.** Nothing model-sized is baked into the pod image.

What the image (`deploy/docker/pod-slim.Dockerfile`) actually contains:
- python:3.12-slim base + GStreamer + libgomp1 + libcudart (~100MB
  compressed total)
- The pod Python package (tiny, ~1MB)
- NO piper/whisper/llama wheels, NO GGUF, NO voice model

What happens at every cold boot (`deploy/docker/slim-boot.sh`):
1. `pip install --no-index` 57 wheels (~160MB) from OUR operator box
   (http://66.94.123.250:8099/wheels-full/) — the step where I-02 stalled
2. Optionally download the 4.9GB GGUF from the same box if not on the
   volume
3. Copy the 63MB Piper voice model from the network volume to local disk
4. Start llama-server + the agent

Why we do NOT bake them in (deliberate, recorded in EP-009 M4 Decision Log):
- **ttl.sh pull speed.** We push to ttl.sh (ephemeral anonymous registry)
  because we have no Docker Hub push creds. Cold-node pulls from ttl.sh are
  slow (~6s/MB observed; the earlier CUDA-fat image took >45 min). Baking
  in 160MB of wheels + 4.9GB GGUF + voice would make the image ~5GB and the
  pull itself becomes the slow step — potentially worse than the current
  runtime download from our mirror (which is fast on HEALTHY nodes).
- **Weights-never-in-image** is an existing design rule (Dockerfile
  comment; weights live on the network volume, mounted read-mostly).
- **Volume persistence.** The GGUF and voice already persist on the network
  volume, so a healthy cold boot pays the 4.9GB only once (download is
  skipped when the file exists). The recurring cost is the 160MB wheel set.

How baking WOULD affect us (if we got fast registry creds, e.g. Docker Hub
or GHCR with a decent mirror):
- **Pro**: eliminate the runtime pip step entirely → cold boot becomes
  "pull image + start process". Removes the exact stall window that killed
  I-02 (wheel fetch from our box mid-boot).
- **Pro**: fewer moving parts at boot; less dependence on the operator box
  being reachable from every node.
- **Con**: image grows ~160MB (wheels) → +~1.5 min on ttl.sh pulls at
  observed rates; baking the 4.9GB GGUF too would add ~50+ min on ttl.sh —
  a net LOSS unless we move to a fast registry.
- **Net recommendation**: bake ONLY the wheel closure (~160MB) into the
  image once we can push to a fast registry (Docker Hub creds or a GHCR
  mirror); keep GGUF + voice on the volume. This shrinks boot to a
  single predictable step and removes the stall window without a 5GB image.

Relevant files to review:
- deploy/docker/pod-slim.Dockerfile (what is/isn't baked)
- deploy/docker/slim-boot.sh (what runs at boot — wheels, GGUF, voice copy)
- deploy/docker/pod.Dockerfile (the full CUDA image, wheels baked at
  build time — we do NOT use this for staging because its 379MB size pulled
  >45 min from GHCR/ttl.sh on cold nodes; EP-009 M4 decision)

---

## Throttling vs node quality — honest read

- No doc states intentional throttling of ttl.sh or operator-box pulls.
  Community threads describe slowdowns correlated with RunPod-side changes
  and per-node variance (pierce.dev link above).
- The crash-loop signature we see (container restarts ~11 min in, zero
  logs, no agent) is consistent with bad-node/init-timeout behavior, NOT
  fixable from the image side. Our healthy pod booted the SAME image in
  ~110s — image/config are not the differentiator; the node is.
- Evidence in I-02 shows the node's egress stalled on a 2.4MB wheel fetch
  from our mirror — a healthy node does this in <1s (we measured 200 in
  7ms from this host). That is a broken node, not throttling of us.

## How to keep this log current
- Append a new `### I-YYYY-MM-DD-NN — title` entry per incident.
- Include pod id, timestamps (local), symptom, evidence file/links, action,
  and YES/NO/UNCERTAIN for billing.
- After the pod is terminated, update the Dispute summary table.

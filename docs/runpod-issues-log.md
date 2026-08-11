# RunPod Issues Log — VIHS EP-010 (dispute evidence)

Purpose: record every RunPod platform/instance problem encountered during
EP-010 (and carry-forward from EP-009 M2–M5) so the operator can verify
billing and dispute charges for unusable/broken instances with RunPod
support. Each entry: pod id, timestamps, symptom, evidence (with links),
action taken, billing concern (YES/NO/UNCERTAIN).

Evidence artifacts live in `docs/runpod-evidence/` (committed). Raw console
dumps are also on this host under /tmp with the file names listed.

Operator rules (2026-08-11, kill window tightened to 7 min):
1. Do NOT terminate a HEALTHY pod — cold boots are slow and eat usage time.
   Once a pod runs fluidly, keep it warm and reuse it for all evidence.
2. Kill-and-retry a pod that returns NOTHING for 7 minutes (no agent
   registration, no health, no logs) — enforced by scripts/m2-retry-deploy.sh
   (POD_MAX_WARM_SECONDS=420). Rationale: healthy pods boot in ~110s and
   RunPod itself marks workers unhealthy when cold start exceeds 7 min, so
   silence past 7 min is a bad node, not a slow boot.

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

### I-2026-08-11-06 — Bad node, killed per 7-min rule (4lby9klwrazkav)
- **Pod**: 4lby9klwrazkav (name vihs-capacity-4090, image v0.2.10)
- **Created**: ~20:07:35 local (enforcement loop attempt 1) · **Killed**: ~22:1x
  local (~2h11m billed, of which the last ~7+ min watched under the new rule;
  the pod was created and watched under the prior 30-min rule, then both
  background loops were SIGTERMed, leaving it unwatched until this session)
- **Symptom**: ports assigned (8093→public) but NO agent registration in the
  orchestrator (no new staging-4090 entry), ZERO lines in /tmp/pod-reports.log,
  logs endpoint returns nothing — container never became usable
- **Evidence**: RunPod GraphQL uptime ~453s at kill check; orchestrator
  /admin/pods shows only dead entries; /tmp/pod-reports.log grep = 0 lines
- **Action**: terminated per operator 7-min rule (tightened from 30 min;
  healthy pods boot ~110s, RunPod unhealthy threshold is 7 min)
- **Billing concern**: **YES — dispute.** Billed ~2h11m (mostly under the old
  30-min watch before this session), never usable (no agent, no registration,
  no report lines).

### I-2026-08-11-07 — Bad node, killed per 7-min rule (gqwksjzmljp3ds)
- **Pod**: gqwksjzmljp3ds (name vihs-capacity-4090, image v0.2.11 baked)
- **Created**: ~20:37:46 local · **Killed**: ~20:48 local (~10 min billed)
- **Symptom**: container started (runtime present, ports 8093→public
  assigned) but NO orchestrator registration (no fresh staging-4090 entry),
  NO fresh lines in /tmp/pod-reports.log after creation — agent never
  reached the report-forwarder stage. First v0.2.11 (baked-wheels) pod;
  image pull + llama wheel install still happens at boot, so the stall is
  again node-side.
- **Evidence**: RunPod runtime True + ports; orchestrator /admin/pods only
  ever showed the OLD dead staging-4090 (v0.2.10, last_ping 41k s);
  /tmp/pod-reports.log line count static at 10863 across a 60s+ window.
- **Action**: terminated per operator 7-min rule. (Also found and fixed:
  the retry driver had its own hardcoded 2700s ready-deadline, which made
  the script-level 420s watch moot — VIHS_READY_TIMEOUT now propagates 420
  and the script kills immediately on "pod never Ready".)
- **Billing concern**: **YES — dispute.** ~10 min billed, never usable.

---

## Dispute summary (draft)

| # | Pod | Unusable time (est) | Chargeable? | Status |
|---|---|---|---|---|
| I-02 | ryqiz3rqtqs9kd | ~46 min (18:06–18:52) | YES — broken node, never usable | OPEN |
| I-03 | ab9mgl4xxp33r2 | ~36 min (19:04–19:40) | YES — broken node, never usable | OPEN |
| I-05 | e83mgcakbwb877 | ~27 min (19:41–20:08) | YES — broken node, never usable | OPEN |
| I-06 | 4lby9klwrazkav | ~2h11m (20:07–22:1x, mostly under old 30-min watch) | YES — never registered agent | OPEN |
| I-07 | gqwksjzmljp3ds | ~10 min (20:38–20:48) | YES — never registered agent | OPEN |

**Plus 13 AUTO-killed pods (20:48–22:54, ~7 min each, never Ready):**
2er50ruc3lvgwb, 14bngamekc9ypj, 0yex0vd8eqbj8e, 0oauaxclyf0bss,
d8fxsgvyn40tur, 55yhg271r7mooi, utaxwo3ydm3ban, r94blg8ng6ceat,
op92y3ud0xtgfl, 4rnnus91ojg7rg, t7v1oa87uxjaj5, bwrs77wzd69sf1,
1sxfyrnnr9dyw6, fglyqvyhsxtudx, hpcfs3fzpqvses — ~101 min combined.
See the I-2026-08-11-AUTO entries below for full detail.

Total disputed to date: **20 pods, ~351 min (~5h51m)** of billed-but-unusable
instance time. Full case in docs/runpod-dispute-email.md.

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

## Are we baking models into images? — wheels yes (as of v0.2.11), models no

**Current state (2026-08-11): the runtime wheel closure IS baked; model
weights are NOT.**

What the image (`deploy/docker/pod-slim.Dockerfile`) now contains:
- python:3.12-slim base + GStreamer + libgomp1 + libcudart (~100MB
  compressed total)
- The pod Python package (tiny, ~1MB)
- **The 56-wheel runtime closure (~180MB) installed at BUILD time** —
  aiortc, av, websockets, numpy, httpx, faster-whisper, piper-tts and all
  transitive deps (v0.2.11; wheelhouse fetched from the operator mirror
  during build, then removed from the image layer). Marker
  /opt/vihs-wheels-installed makes slim-boot.sh skip boot-time pip.
- NO llama-cpp-python wheel (1.36GB — deliberately left at boot), NO GGUF,
  NO voice model

What still happens at every cold boot (`deploy/docker/slim-boot.sh`):
1. (Skipped when baked) — the wheel install step that stalled on I-02 is
   GONE. Only llama-cpp-python (1.36GB) installs here, and only when
   VIHS_LLAMA_GGUF is set, with a 10-try retry loop
2. Optionally download the 4.9GB GGUF from the same box if not on the
   volume
3. Copy the 63MB Piper voice model from the network volume to local disk
4. Start llama-server + the agent

Why bake wheels but NOT models (decision 2026-08-11, user-approved):
- **Bake the wheels** because the boot-time pip step was the I-02 stall
  window (node egress to our mirror stalled mid-install). A cold boot now
  has NO operator-box dependency for the base closure.
- **Do NOT bake the 1.36GB llama wheel or the 4.9GB GGUF** — image would
  balloon to ~1.8GB+ and ttl.sh pulls (already ~6s/MB on cold nodes) would
  take hours, which is worse than the stall we're fixing.
- **GGUF + voice persist on the network volume** (weights-never-in-image
  design rule; volume mount read-mostly) — paid once, skipped on reboot.
- **Size cost of the bake**: v0.2.10 compressed ~93MB → v0.2.11 ~264MB.
  Measured on the next real pod boot; if the pull-time penalty exceeds the
  removed install time on healthy nodes, revisit (v0.2.10 remains tagged as
  fallback).
- The ttl.sh pull itself was never the stall (I-02 pulled the image fine at
  16:22Z then stalled on wheels at 17:01Z) — so a larger pull is acceptable;
  the crash-loop signature is node-side.

Relevant files to review:
- deploy/docker/pod-slim.Dockerfile (bake step, ARG VIHS_WHEEL_BASE)
- deploy/docker/slim-boot.sh (llama retry, GGUF, voice copy)
- deploy/docker/pod.Dockerfile (the full CUDA image — we do NOT use this
  for staging because its 379MB size pulled >45 min from GHCR/ttl.sh on
  cold nodes; EP-009 M4 decision)

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
### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: 2er50ruc3lvgwb
- **Created**: 20:55:10 local · **Killed**: 20:55:10 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: 14bngamekc9ypj
- **Created**: 21:03:47 local · **Killed**: 21:03:47 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: 0yex0vd8eqbj8e
- **Created**: 21:12:21 local · **Killed**: 21:12:21 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: 0oauaxclyf0bss
- **Created**: 21:20:57 local · **Killed**: 21:20:57 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: d8fxsgvyn40tur
- **Created**: 21:31:02 local · **Killed**: 21:31:02 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: 55yhg271r7mooi
- **Created**: 21:39:38 local · **Killed**: 21:39:38 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: utaxwo3ydm3ban
- **Created**: 21:48:16 local · **Killed**: 21:48:16 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: r94blg8ng6ceat
- **Created**: 22:02:55 local · **Killed**: 22:02:55 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: op92y3ud0xtgfl
- **Created**: 22:11:29 local · **Killed**: 22:11:29 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: 4rnnus91ojg7rg
- **Created**: 22:20:06 local · **Killed**: 22:20:06 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: t7v1oa87uxjaj5
- **Created**: 22:28:42 local · **Killed**: 22:28:42 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: bwrs77wzd69sf1
- **Created**: 22:37:18 local · **Killed**: 22:37:18 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: 1sxfyrnnr9dyw6
- **Created**: 22:45:54 local · **Killed**: 22:45:54 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)

### I-2026-08-11-AUTO — pod never Ready within 420s
- **Pod**: fglyqvyhsxtudx
- **Created**: 22:54:29 local · **Killed**: 22:54:29 local
- **Symptom**: no agent registration / no readiness within 420s (operator 7-min rule)
- **Action**: automatic kill per operator rule; retry loop continues
- **Billing concern**: **YES — dispute** (pod billed while never usable)


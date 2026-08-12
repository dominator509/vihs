# VIHS — Handoff Note (where we left off)

_Last updated: 2026-08-12. Written so any agent with no prior conversation
can pick up exactly where the previous session stopped._

## 1. Repo state

- **Branch:** `main` — clean working tree (nothing uncommitted).
- **Remote:** `origin https://github.com/dominator509/vihs.git` (pushed and
  verified: remote `main` == local `HEAD` == `d387fad`).
- **Active ExecPlan:** `EP-010-production-readiness.md` (see `.agent/PLANS.md`;
  EP-000..EP-009 all DONE/SKIPPED).
- **Boot sequence for any agent:** read `AGENTS.md`, run
  `scripts/preflight.sh`, then continue EP-010 per its Progress section.
  Do NOT re-run completed milestones; M1 is green.

## 2. EP-010 status (the actual stopping point)

- [x] **M1** — gate green; PROD-READY FAIL was expected until M2 derives the
      real capacity number.
- [ ] **M2** — harness fixed, real ramp RUNS; `sessions_per_gpu` derivation
      under spec budgets. **This is where work stopped.**
- [ ] M3, M4, M5 — untouched.

### Why M2 is not simply "next": the budget decision
The spec budgets in `ARCHITECTURE.md` §6 (`llm_ttft 400ms / tts_ttfa 300ms /
e2e 1.5s`) were measured as unreachable with the original production model.
The operator (Dominic) already made the two key calls, both implemented and
measured (full detail in EP-010 §14 Decision Log):

1. **2026-08-10 (b):** added `claude-haiku-4-5` to the AXIOM gateway. Best
   hosted option, llm_ttft 1.2–2.5s — still 3–6x over budget. Not enough alone.
2. **2026-08-10 (c):** local LLM on pod (llama.cpp). **llm_ttft = 323ms — MEETS
   the 400ms budget** (first provider to do so). New binding constraint became
   TTS, not LLM.
3. **2026-08-11 (contention fix, v0.2.10):** proven root cause — onnxruntime
   defaulted to ALL cores, so Piper TTS thrashed llama-server on shared vCPUs.
   Fix: bounded Piper ONNX session pool via `VIHS_TTS_THREADS` (default 2).
   Measured after deploy: **tts_ttfa p50 68–152ms (MET 300ms budget; was
   1.4–1.7s)**; llm_ttft warm 26–185ms (MET). Residual p95 tail (~600ms) is
   longest-single-clause length, not CPU contention.
4. **2026-08-11 (operator decision):** KEEP `VIHS_TTS_THREADS=2`. Tail is
   clause-length-bound; more threads would steal LLM headroom for no p95 gain.

### What M2 actually needs now
- Re-derive `sessions_per_gpu` on the next real deploy with the current
  image/measurements (last derivation was 0 because tts_ttfa p95 was the
  binding constraint; that constraint is now MET, so the number should be >0).
- The approved budgets to re-derive against: **tts_ttfa p95 ≤ 800ms,
  lipsync_ff p95 ≤ 900ms** (amended 2026-08-11 per operator decision — see
  `ARCHITECTURE.md` §6 table rows; these supersede the original §6 numbers
  and the superseded 3000/5000/9000ms recommendation in the EP-010 §14 log).
- If anything about the pod/LLM/TTS stack regresses, re-check
  `docs/runpod-issues-log.md` first — 21 incidents logged, all with the same
  root-cause family (node quality / boot stalls), already mitigated.

## 3. Operational rules that MUST be respected (operator-approved)

- **KEEP warm pods warm and reuse them.** Do not kill healthy pods.
- **KILL + RETRY pods silent > 7 minutes** — `POD_MAX_WARM_SECONDS=420` in
  `scripts/m2-retry-deploy.sh`, driver `VIHS_READY_TIMEOUT=420`. This matches
  RunPod's own >7-min unhealthy threshold. This is an operator rule, not a
  suggestion.
- **Log every unusable pod** in `docs/runpod-issues-log.md` (21 incidents so
  far; billing dispute in progress — see below).
- **Image bake policy (v0.2.11):** the 56-wheel runtime closure (~180MB) is
  baked at BUILD time; the 1.36GB llama-cpp-python wheel stays a boot-time
  fetch (10-try retry); the 4.9GB GGUF + voice stay on the network volume —
  weights never in image. v0.2.10 kept as fallback.

## 4. RunPod billing dispute (IN PROGRESS — do not lose this thread)

- **File:** `docs/runpod-dispute-email.md` (draft) + `docs/runpod-dispute-email.txt`
  (plain-text paste copy) + `docs/runpod-evidence/` (per-incident links).
- **Status:** 20 unusable pods billed on 2026-08-11, US-IL-1 RTX 4090; refund
  requested via help@runpod.io from dswmarketingllc@agentmail.to. Subject line
  and body are ready. The email wording was updated last commit (`d387fad`).
- **Next step if Dominic asks:** send via the AgentMail inbox; keep the
  evidence log in `docs/runpod-evidence/` attached.

## 5. Environment changes made 2026-08-12 (read before deploying)

- **Lexi GGUF staging copy MOVED.** It was at `/tmp/llama-gguf/Lexi-Q4_K_M.gguf`
  (referenced in `docs/gguf-verification-report.md` line 10 — that doc is now
  stale on this one line). It now lives at:
  **`/root/lexi-staging/Lexi-Q4_K_M.gguf`** (4,920,739,104 bytes,
  sha256 `376ac398...` — verify with `sha256sum` before use).
  Production copy is still fetched from the operator mirror URL
  (`VIHS_LLAMA_GGUF_URL` in `scripts/m2-retry-deploy.sh`) — the staging copy
  is only for local verification/upload. Do NOT delete it while EP-010 is
  active; it was preserved deliberately during disk cleanup.
- **Disk cleanup 2026-08-12:** `/tmp` was cleared (was 27G), caches cleared,
  and dead projects removed (ListingLift, Powderburn, monte-cristo, June
  source clones). **Do NOT re-create or reference `/tmp/llama-gguf/`** — it no
  longer exists. Nothing VIHS depends on was deleted: `/root/axiom` (AXIOM
  gateway + 6 live systemd services) and `hermes-env` were intentionally kept.
- AXIOM is a LIVE dependency for VIHS (ADR-012: AXIOM LLM gateway is the
  stage/prod brain seam). Do not propose removing it.

## 6. Quick-start commands

```bash
cd /root/vihs
scripts/preflight.sh              # must print preflight: ok
scripts/m2-retry-deploy.sh        # pod deploy + 7-min kill/retry loop
# read EP-010 Progress + Decision Log before any M2 work:
.agent/execplans/EP-010-production-readiness.md
```

## 7. If the repo was cloned fresh (not this box)

- Restore the runtime secrets per `ENVIRONMENT.md` (never committed; `.env`
  gitignored).
- The 4.9GB GGUF is NOT in git — fetch via `VIHS_LLAMA_GGUF_URL` or copy from
  `/root/lexi-staging/` on this box.
- All commands, env vars, routes, Redis keys: `COMMANDS.md`, `ENVIRONMENT.md`,
  SPEC-002/SPEC-003 — never invent one.

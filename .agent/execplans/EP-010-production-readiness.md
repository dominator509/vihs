# EP-010 — Production Readiness

## 1. Purpose / Big Picture
Run the full gauntlet from SPEC-008/PRODUCTION_READINESS.md and assemble the
evidence for the launch gate. Nothing new gets built here except fixes for
what the drills expose.

## 2. Scope
Execute + evidence: full verification on the release tag; capacity derivation
on production models (real loadtest-capacity run); staging chaos drills
(kill-pod, memoryd restart) with real stages; security drills incl.
hard-delete and foreign-owner probes against staging; backup restore drill;
rollback drill (if not fresh from EP-009); docs/runbook review; readiness
report; launch ADR draft.

## 3. Non-goals
The production deploy itself (operator-executed under STOP S6 after
sign-off). Feature work. Perf work beyond fixing readiness blockers.

## 4. Context and Orientation
(see below)

## 5. Files to Read First
PRODUCTION_READINESS.md (the checklist IS the plan's skeleton), SPEC-008,
all runbooks, EP-007 chaos harness, EP-009 staging setup.

## 6. Files to Change
PRODUCTION_READINESS.md (checkboxes + evidence links), DECISIONS.md (launch
ADR draft), fixes' files as drills demand (each fix logged + tested),
readiness report at docs/readiness-vX.Y.Z.md.

## 7. Interfaces and Contracts
Evidence artifact per checklist row: command output, dashboard snapshot, or
drill log, linked from the checklist.

## 8. Milestones
M1 Machine-checkable pass: `sh scripts/production-readiness-check.sh` on the
   tag. Expected: PROD-READY OK for automated subset.
M2 Capacity derivation on production model set (real GPU). Expected:
   report + POD_MAX_SESSIONS set from it (never ADR-010 default). S1-gated.
M3 Staging drills: kill-pod-midturn (real), memoryd restart, latency SLI on
   a FULL pod, cold-start p95. Expected: SPEC-008 P3/P4 evidence.
M4 Security + data drills: authz probes, hard delete sweep-verified, backup
   restore to scratch + fleet fsck. Expected: P5/P6 evidence.
M5 Docs/runbooks/rollback confirmation + readiness report + launch ADR
   draft. Expected: every checklist row checked w/ link; operator sign-off
   requested (STOP S6 boundary reached = plan complete).

## 9. Concrete Steps
Checklist order; any failed row → fix under this plan with its own logged
mini-milestone, re-drill, then continue.

## 10. Validation and Acceptance
Acceptance: PRODUCTION_READINESS.md fully evidenced; PROD-READY OK; launch
ADR drafted; open risks enumerated in the report.

## 11. Idempotence and Recovery
(re-runnable as stated)

## 12. Progress
All drills re-runnable.
- [x] M1 (gate green; PROD-READY FAIL expected until M2 derives cap)
- [ ] M2 (harness FIXED + real ramp RUNS; derivation = 0 under spec budgets — BLOCKED on LLM-latency decision, see §14)
- [ ] M3 - [ ] M4 - [ ] M5

## 13. Surprises & Discoveries
- Relay pod-ward dial had NO timeout: a hung dial left the client WS silent
  (state frame never sent). Fixed with a 10s timeout + dial logging
  (2ca3f0f).
- RunPod proxy URLs (https://{pod}-{port}.proxy.runpod.net) sit behind
  Cloudflare: the default Python-urllib UA gets 403/1010. Pod-ward HTTP in
  the harness now sends a browser-like UA (2ca3f0f).
- Local CI capacity test is contaminated by any registered REMOTE pod: the
  scheduler assigns sessions across ALL ready pods, so a keep-warm staging
  pod steals a stage's session and the local pod's metrics never settle.
  Run local CI with zero remote pods registered.
- FIRST REAL-GPU DERIVATION: the production LLM path (ADR-012 AXIOM gateway
  → Lightning claude-opus-4-7) measures llm_ttft 1467–1850ms vs the 400ms
  budget (4.6×); relaxing LLM budget to 2000ms exposes tts_ttfa 4424ms
  (budget 300ms) and e2e_first_frame/total ≈ 6.2s (target 1.5s). Breach at
  ONE session under every spec-realistic budget → sessions_per_gpu = 0.
  ARCHITECTURE §6's levers (model size, prefix cache) assume a vLLM-class
  self-hosted model, not a hosted frontier model.

## 14. Decision Log
- M2 BLOCKED (STOP S4): the spec latency budgets (ARCHITECTURE §6,
  llm_ttft 400ms / tts_ttfa 300ms / e2e 1.5s) are unreachable with the
  chosen production model. Options: (a) amend budgets to the model's
  measured reality, (b) add a faster model to the AXIOM gateway, (c)
  self-host a fast vLLM model (the spec's original lever). Operator
  decision required — no spec resolves which path.

## 15. Outcomes & Retrospective

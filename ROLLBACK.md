# ROLLBACK.md — VIHS

## Triggers
- Post-deploy smoke fails in production.
- SEV1 (data integrity: chain break attributable to release, authz defect).
- e2e latency SLO breach sustained >30 min attributable to release.
- Error-rate page not mitigated within 30 min.

## Decision owner
Operator (djw). Agents may PREPARE a rollback (previous tag identified,
commands staged) but never execute against production (STOP S6).

## Rollback types & methods
- Application rollback (default): redeploy previous tag — control plane
  binaries first (memoryd → orchestrator), then pod template pinned to
  previous image; force-drain pods only if the defect is in the pod.
- Config rollback: revert env change; restart affected service.
- Feature-flag rollback: v1 has one flag class (`VIHS_REAL_STAGES`, staging
  only); no prod flags to flip.

## Database considerations
No relational DB. Event log is append-only and version-tolerant one `v` step;
rollback never mutates data. If the defective release wrote events at a new
`v`, the previous reader tolerates them (tolerant-reader rule, SPEC-002); if
it wrote CORRUPT events, isolate affected sessions (deny resume) and record a
SEV1 — do not delete or rewrite (chain evidence).

## Verification after rollback
smoke-test.sh green; dashboards recovered to pre-release baselines for 30
min; chain-fsck sample across sessions active during the incident.

## Verified by drill (EP-009 M5, 2026-08-10)
`deploy/runpod/rollback-drill.py` exercises the application-rollback leg on
staging against REAL pods: deploy current image tag → smoke (release
assertion) → deploy PREVIOUS image tag → smoke (release assertion) → restore
current. The smoke's `--expect-release TAG` (smoke-test.sh
`VIHS_EXPECT_RELEASE`) asserts the pod registered the expected tag
(`versions.pod`, exposed in GET /admin/pods), proving WHICH image is live —
not just that smoke passes. Result: rollback leg SMOKE OK in **97.8s** (budget
600s), 0 pods left billing after each leg. Control-plane binary rollback
(memoryd → orchestrator) is the same redeploy-previous-tag path and was not
separately exercised in the drill because the v0.1.0↔v0.2.0 control-plane
delta is the additive `versions` field the assertion reads.

## Communication
Staging: note in ops channel. Production: incident entry + user-visible
status if downtime exceeded 5 min.

## Postmortem
Within 48 h for any production rollback: timeline, root cause, why tests
missed it, new regression test added (test-first rule), action items in
DECISIONS/ExecPlan.

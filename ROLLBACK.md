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

## Communication
Staging: note in ops channel. Production: incident entry + user-visible
status if downtime exceeded 5 min.

## Postmortem
Within 48 h for any production rollback: timeline, root cause, why tests
missed it, new regression test added (test-first rule), action items in
DECISIONS/ExecPlan.

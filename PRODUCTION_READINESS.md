# PRODUCTION_READINESS.md — VIHS

Production readiness = ALL sections below pass, verified by
`scripts/production-readiness-check.sh` plus the manual drills listed. The
final launch gate is a recorded sign-off by the operator.

## Functional readiness
[ ] All five core user outcomes (PROJECT_BRIEF) demonstrated end-to-end on
staging hardware. [ ] All SPEC required behavior implemented. [ ] Non-goals
still excluded (grep gate: no billing, no group-call, no mobile code paths).
[ ] Known critical bugs resolved or explicitly accepted in writing.

## Test readiness
[ ] lint, format, typecheck, unit, integration, contract, E2E, build, smoke
all green via verify.sh. [ ] Invariant regression suite green (INV-1..5).
[ ] Chaos suite green: pod kill mid-turn resumes; memoryd restart mid-append
no torn chain; torn-write injection caught by fsck.

## Security readiness
[ ] SECURITY.md checklist complete. [ ] Ownership-gated resume verified with
negative tests. [ ] Pod credential scoping verified (pod token cannot touch a
second session). [ ] Encryption at rest confirmed on target store.
[ ] Hard-delete drill leaves zero objects + zero index keys.

## Privacy / data readiness
[ ] Retention TTL sweep tested. [ ] Export (transcript.md) tested.
[ ] Deletion audit line emitted. [ ] Backup snapshot + restore drill done
(restore to scratch bucket, full-fleet chain-fsck green).

## Performance readiness
[ ] Capacity cap DERIVED by loadtest-capacity.sh on production model set and
configured (never the ADR-010 default). [ ] e2e p50 ≤ 1.3 s / p95 ≤ 2.0 s on a
full pod. [ ] Abort-flush ≤ 100 ms p95 under load. [ ] Cold start ≤ 30 s p95
and masked by client idle loop.

## Accessibility readiness (client)
[ ] Keyboard: connect/disconnect/resume operable. [ ] Captions toggle renders
live transcript text. [ ] Status not conveyed by color alone. [ ] Semantic
landmarks/labels on controls.

## Observability readiness
[ ] OBSERVABILITY.md acceptance met; dashboards live; alerts loaded and
test-fired once; prefix-cache ratio visible ≥0.9 in staging soak.

## Deployment & rollback readiness
[ ] Staging deploy from CI succeeded twice consecutively. [ ] Post-deploy
smoke automated. [ ] Rollback drill executed on staging (previous tag
redeployed, smoke green, ≤10 min). [ ] Release checklist + approvals wired.

## Documentation & support readiness
[ ] All root docs current (spot check by final-review checklist). [ ] Runbooks
cover every alert. [ ] Escalation path named. [ ] Known risks listed in
EP-010 Outcomes.

## Final launch gate
Operator reviews this file with evidence links, signs off in DECISIONS.md as
an ADR ("Launch vX.Y.Z"), production deploy proceeds under STOP S6 rules.

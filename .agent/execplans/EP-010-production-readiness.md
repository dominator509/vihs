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
All drills re-runnable. - [ ] M1 - [ ] M2 - [ ] M3 - [ ] M4 - [ ] M5

## 13. Surprises & Discoveries
## 14. Decision Log
## 15. Outcomes & Retrospective

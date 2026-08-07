# SPEC-008 — Production Readiness Conditions

Status: accepted · Owner: djw · Roadmap phases: 8–9 · Linked ExecPlans: EP-009, EP-010

## User-visible goal
Launch only when the product keeps its three promises under stress: fast,
interruptible, unforgettable (and deletable).

## Non-goals
Multi-region; formal compliance certifications (self-hosted operator scope).

## Required behavior (conditions; PRODUCTION_READINESS.md is the checklist)
- P1 verify.sh + invariant + chaos suites green on the release tag.
- P2 Capacity cap DERIVED on the production model set via
  loadtest-capacity.sh and configured; router enforcement test green.
- P3 Latency SLIs within budget on a FULL pod (not an idle one).
- P4 Kill-pod-mid-turn drill: user resumes, transcript honors INV-1, no
  torn chain fleet-wide (chain-fsck sweep).
- P5 Security drills: foreign-owner matrix, pod-token boundary, signed-URL
  expiry, hard-delete leaves zero artifacts, secrets scan clean.
- P6 Restore drill from backup snapshot to scratch bucket; fsck sweep green.
- P7 Rollback drill on staging ≤10 min to previous tag with smoke green.
- P8 Runbooks cover every alert; docs current; launch ADR recorded.

## Inputs/outputs
Evidence artifacts (reports, dashboard snapshots, drill logs) linked from the
EP-010 Outcomes section.

## Error states
Any failed condition returns EP-010 to the owning phase's ExecPlan with a
logged deficiency; no partial launches.

## Required tests
`scripts/production-readiness-check.sh` automates the machine-checkable
subset (P1, parts of P2/P5); drills are scripted-but-human-witnessed.

## Acceptance criteria
Operator sign-off ADR "Launch vX.Y.Z" recorded in DECISIONS.md with evidence
links; STOP S6 rules honored for the deploy itself.

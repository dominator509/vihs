# SPEC-007 — Observability

Status: accepted · Owner: djw · Roadmap phase: 7 · Linked ExecPlans: EP-005 (instrumentation hooks), EP-008

## User-visible goal (operator-visible)
When the pipeline blows its budget, the guilty stage names itself. When
memory misbehaves, the graphs say so before users do.

## Non-goals
Third-party APM requirement; per-user analytics; content capture.

## Normative registries
Metric names, labels, log fields, alert thresholds: OBSERVABILITY.md is the
implementation registry; additions land here + there in the same change.
Required coverage (must exist, exact names in OBSERVABILITY.md):
per-stage first-chunk histograms; e2e first-audio; barge-in count +
abort-flush ms; premature-endpoint count; append latency + pod buffer depth;
prefix-cache hit ratio (INV-4 validation); compaction count + blob tokens;
pod fill vs cap + GPU util; cold-start histogram; scale events; resume
results; authz denials.

## Required behavior
- O1 Every stage wrapper times first-chunk and full-span; histogram labels
  `pod_id`, `model_ver`, `stage`.
- O2 Redaction enforced by a log middleware with unit tests (forbidden-class
  strings in test fixtures must be dropped/masked).
- O3 /healthz (liveness) and /readyz (dependency) on both control services;
  pod /health with per-stage readiness.
- O4 Dashboards + alert rules as code in `deploy/observability/` (loaded in
  EP-008), thresholds initial per OBSERVABILITY.md.
- O5 Prefix-cache ratio sourced from the LLM server's stats endpoint and
  attributed per pod; an epoch-boundary annotation metric
  (`vihs_epoch_boundary_total`) lets dips be explained.

## Error states
Metrics endpoint failure never affects the media path (isolated task).

## Required tests
Redaction tests; metric-presence smoke (scrape after E2E run asserts all
required series exist); readyz dependency matrix test.

## Acceptance criteria
OBSERVABILITY.md acceptance section satisfied; metric-presence smoke green in
CI; one full staging soak shows cache ratio ≥0.9 between epochs.

# EP-008 — Observability & Operations

## 1. Purpose / Big Picture
Make the pipeline visible and operable: full metric registry emitted,
structured logging + redaction verified everywhere, dashboards + alerts as
code, runbooks finished, metric-presence smoke in CI.

## 2. Scope
Prometheus exporters in orchestrator/memoryd/pod; the complete
OBSERVABILITY.md metric set (incl. prefix-cache ratio from vLLM stats and
epoch-boundary annotations); /readyz dependency checks; dashboards + alert
rules in deploy/observability/ (Grafana/prom rule YAML); runbook per alert
in OPERATIONS.md; metric-presence smoke.

## 3. Non-goals
Tracing rollout (optional per SPEC-007); paging integration (operator infra);
log shipping stack (stdout JSON is the contract).

## 4. Context and Orientation
(see below)

## 5. Files to Read First
SPEC-007 (normative), OBSERVABILITY.md (registry), OPERATIONS.md (tables to
complete), EP-005 metric hooks already emitting stage histograms.

## 6. Files to Change
metrics modules in all three services, deploy/observability/{dashboards/*.json,
alerts.yml}, scripts/smoke-test.sh (add metric-presence assertions),
OPERATIONS.md (alert→runbook links), OBSERVABILITY.md (any name corrections —
registry stays truthful).

## 7. Interfaces and Contracts
/metrics on each service; names EXACTLY per registry; smoke scrapes after the
E2E convo and asserts every required series exists with >0 samples.

## 8. Milestones
M1 Control-plane metrics + readyz matrices. Validation: unit + scrape test.
M2 Pod metrics incl. cache-ratio poller + epoch annotations. Validation:
   pipeline flow test asserts series.
M3 Dashboards + alert rules load in a compose grafana/prom (dev profile).
   Validation: `sh scripts/dev-services.sh up-obs` loads clean; screenshots
   linked in Outcomes.
M4 Metric-presence smoke + runbook links. Validation: `sh scripts/smoke-test.sh`
   SMOKE OK with presence checks; every alert names a runbook anchor.

## 9. Concrete Steps
Milestone order; metrics tasks isolated from media path (SPEC-007 error rule).

## 10. Validation and Acceptance
SMOKE OK + VERIFY OK; acceptance = SPEC-007 acceptance criteria.

## 11. Idempotence and Recovery
(re-runnable as stated)

## 12. Progress
Re-runnable. - [ ] M1  - [ ] M2  - [ ] M3  - [ ] M4

## 13. Surprises & Discoveries
## 14. Decision Log
## 15. Outcomes & Retrospective

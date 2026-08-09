# OPERATIONS.md — VIHS Runbook

## Local operations
Start/stop per COMMANDS.md. State reset: `dev-services.sh down && up` wipes
local Redis/MinIO (dev only — forbidden pattern anywhere else).

## Staging / production operations
- Services: orchestrator (:8080 public via proxy, :8081 admin local), memoryd
  (:8091 internal only), Redis, MinIO/S3, coturn, provider pods.
- Health checks: `GET /healthz` on orchestrator and memoryd (liveness);
  orchestrator `GET /admin/pods` shows pod registry with last-ping age (pods
  ping every 5 s; stale >15 s ⇒ marked unhealthy). Pod exposes `/health`
  reporting stage readiness + slot fill.

## Common failure modes → actions
| Symptom | Likely cause | Action |
|---|---|---|
| First-audio latency spike, one pod | over-admission or model swap w/o cap re-derivation | check `/admin/pods` fill vs POD_MAX_SESSIONS; drain pod; rerun capacity test |
| All sessions on a pod drop | pod crash | autoscaler auto-replaces; verify users resumed (resume-success metric); nothing manual unless replacement loops |
| Appends buffering on pods (pod health degraded) | memoryd down/slow | restart memoryd (writers replay tips from log — safe); watch buffer drain metric |
| Redis lost | infra | memoryd `--rebuild-index` from object store (ADR-003); latency degraded during rebuild, zero data loss |
| chain-fsck failure on a session | torn write or tampering | isolate session (deny resume), export log, investigate; NEVER hand-edit the log |
| Cold starts >30 s | volume mount slow / snapshot cold | check provider volume health; verify warm-pool floor honored |
| TURN-only users failing | coturn creds/ports | validate TURN_URL creds; check 3478/49152+ range open |

## Alert → runbook anchors (EP-008 M3)
Each alert rule in deploy/observability/alerts.yml maps to failure-mode rows
above; full runbook text lands in M4.

- `VIHS_E2E_P95_TOO_HIGH` (e2e p95 first-audio > 2000 ms, 10 min) → row 1
  (first-audio latency spike) + row 5 (cold starts).
- `VIHS_ABORT_FLUSH_P95_TOO_HIGH` (abort_flush p95 > 100 ms, 10 min) → row 3
  (appends buffering / memoryd slow) — barge-in feel is the user-facing signal.
- `VIHS_PREFIX_CACHE_RATIO_DROPPED` (ratio < 0.9, 15 min) → INV-4 drift; check
  vLLM prefix cache + VIHS_VLLM_STATS_URL poller (EP-009 real mode).
- `VIHS_APPEND_BUFFER_DEPTH_HIGH` (depth > 32 on any pod) → row 3 (memoryd
  trouble): restart memoryd, writers replay tips from log.
- `VIHS_RESUME_DENIED_SPIKE` (>10 denied/min) → security review; see Incident
  triage SEV1 (wrong-owner access).
- `VIHS_REPLACEMENT_LOOP` (pod replacements 3×) → row 2 (pod crash loop):
  drain, verify resume-success metric, check capacity/volume health.

## Database backup/restore
Object store IS the database. Backups: bucket replication/snapshot per
operator infra (documented target: daily snapshot, 7-day retain in stage,
operator-defined in prod). Restore drill = part of EP-010: restore snapshot to
a scratch bucket, run chain-fsck across all sessions, spot-render transcripts.
Redis is not backed up (rebuildable).

## Scheduled jobs
memoryd retention sweep (TTL) daily at low-traffic hour; compaction is
event-driven (turn boundaries / token budget), not scheduled.

## Incident triage
1 Detect (alert/report) → 2 classify severity: SEV1 = data-integrity (chain
break, wrong-owner access), SEV2 = availability (control plane down), SEV3 =
degraded latency → 3 mitigate using table above → 4 communicate → 5 verify
(smoke + dashboards) → 6 document (postmortem within 48 h for SEV1/2).
Checklist: .agent/checklists/incident-response.md.

## Escalation
Operator (djw) is decision owner for SEV1/2 and for any action touching
stored session data. Coding agents never mitigate by deleting/modifying
session objects (AGENTS.md §13).

## Maintenance windows
Control-plane restarts are seconds and safe any time (buffered appends).
Pod-template rolls prefer natural churn; forced drains announced in staging.

## Operational safety rules
No manual writes to `sessions/` prefixes, ever. Admin listener never exposed
publicly. Any prod-affecting command is run by the operator, not agents.

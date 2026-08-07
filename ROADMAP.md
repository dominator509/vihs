# ROADMAP.md — VIHS

Do not implement directly from this file. Implementation must happen through
an ExecPlan. This roadmap only sequences phases and links specs/ExecPlans.

Ordering follows the de-risking sequence in ARCHITECTURE.md: vertical slice
first (prove the latency budget), then memory/resume, then compaction/prefix
validation, then orchestration/autoscaling, then hardening.

| Phase | Purpose | Depends on | Exit criteria | Specs | ExecPlans |
|---|---|---|---|---|---|
| 0 Discovery & foundation | Confirm greenfield, stand up workspace, toolchain, CI, verify script | — | `verify.sh` green on empty skeleton; preflight passes | SPEC-000 | EP-000, EP-001 |
| 1 Core domain | Event schema, canonical encoding, hash chain, deterministic render, ID types — pure logic, zero I/O | 0 | `vihs-core` unit tests + render determinism + fsck property tests green | SPEC-001, SPEC-002 | EP-002 |
| 2 Data & persistence | memoryd: single-writer append, object store + Redis index, load/render, rebuild-index, compaction | 1 | Integration tests vs local Redis/MinIO green; chain-fsck on generated logs OK | SPEC-002, SPEC-003 | EP-003 |
| 3 API/service layer | Orchestrator: auth gateway, session API, signaling relay, router, pod registry, autoscaler w/ mock provider; **MCP server (mcpd) exposing the same ops as vihs_* tools (ADR-011)** | 2 | Contract tests for all SPEC-003 routes AND MCP tool fixtures green; autoscaler sim tests green | SPEC-003, SPEC-005 | EP-004 |
| 4 Client + pipeline | Pod agent streamed pipeline (mock GPU stages in CI, real stages behind flags), endpointing, barge-in, INV-1/INV-4 tests; static web client | 3 | E2E loopback: scripted turn + barge-in + resume, all green with mocks | SPEC-004, SPEC-001 | EP-005 |
| 5 Auth & security | Token issuance/verification, ownership gating, signed URLs, redaction, hard delete, retention TTL | 4 | SPEC-005 acceptance + security-check.sh green | SPEC-005, SPEC-006 | EP-006 |
| 6 Testing hardening | Failure-mode tests (pod kill, memoryd blip, torn-write injection), capacity load test derives real cap, flaky policy | 5 | verify.sh + loadtest capacity report; chaos suite green | SPEC-006 | EP-007 |
| 7 Observability & ops | Metrics, structured logs, dashboards, alerts, runbooks | 6 | SPEC-007 acceptance; smoke shows metrics | SPEC-007 | EP-008 |
| 8 Deployment & release | Pod Docker image, RunPod templates, network-volume layout, CI/CD, release+rollback flows | 7 | Staging deploy + post-deploy smoke green | SPEC-008 | EP-009 |
| 9 Production readiness | Full drill: chaos, rollback, docs, launch gate | 8 | production-readiness-check.sh: PROD-READY OK | SPEC-008 | EP-010 |

Production readiness milestone = Phase 9 exit = PRODUCTION_READINESS.md final
launch gate signed off.

# EP-008 — Observability & Operations

## 1. Purpose / Big Picture
Make the pipeline visible and operable: full metric registry emitted on all
three services, structured logging + redaction verified everywhere (already
done in EP-006/EP-007), dashboards + alerts as code, runbooks finished,
metric-presence smoke in CI.

## 2. Scope
Prometheus exporters in orchestrator/memoryd/pod; the complete
OBSERVABILITY.md metric set (incl. prefix-cache ratio from vLLM stats and
epoch-boundary annotations); /healthz + /readyz on orchestrator (memoryd
already has both); dashboards + alert rules in deploy/observability/
(Grafana/prom rule YAML); runbook per alert in OPERATIONS.md; metric-presence
smoke.

## 3. Non-goals
Tracing rollout (optional per SPEC-007); paging integration (operator infra);
log shipping stack (stdout JSON is the contract); Grafana auth/SSL (dev
profile only — operator infra in prod).

## 4. Context and Orientation
Current state at plan start:
- memoryd HAS /healthz (liveness) + /readyz (Redis ping → 503) (api.rs).
- orchestrator has NO /healthz, NO /readyz, NO /metrics. Two listeners:
  public :8080 (VIHS_ORCH_ADDR) and admin :8081 (VIHS_ADMIN_ADDR, 127.0.0.1).
- pod has GET /health (stage readiness + fill/cap) but no /metrics.
- No prometheus dependency anywhere in the workspace (Cargo.lock grep = 0).
- deploy/docker/compose.dev.yml = redis + minio + mc only; no obs profile.
- deploy/ has docker/ + runpod/ only; no observability/ dir.
- smoke-test.sh runs run_e2e.py --smoke then prints SMOKE OK — no metric
  assertions.
- OPERATIONS.md failure-mode table has no runbook anchors per alert.
- pod Metrics class (pod/vihs_pod/metrics.py) is pure-stdlib in-memory
  percentile report; no Prometheus text exposition.

Metric ownership (registry OBSERVABILITY.md):
- Orchestrator: vihs_pod_sessions{pod_id}, vihs_scale_events_total{dir},
  vihs_cold_start_secs (hist), vihs_resume_total{result},
  vihs_authz_denials_total.
- memoryd: vihs_append_latency_ms (hist), vihs_compactions_total,
  vihs_memory_blob_tokens, vihs_epoch_boundary_total, vihs_authz_denials_total.
- pod: vihs_stage_first_chunk_ms{stage,pod_id,model_ver}, vihs_e2e_first_audio_ms,
  vihs_bargein_total, vihs_abort_flush_ms (hist), vihs_endpoint_premature_total,
  vihs_append_buffer_depth, vihs_prefix_cache_hit_ratio, vihs_gpu_util,
  vihs_cold_start_secs is orchestrator-side; pod reports per-stage readiness.
  vihs_epoch_boundary_total is emitted by pod when a new epoch memory blob is
  loaded (annotation) AND by memoryd on compaction (the boundary event).

## 5. Files to Read First
SPEC-007 (normative), OBSERVABILITY.md (registry), OPERATIONS.md (tables to
complete), EP-005 metric hooks (pod/vihs_pod/metrics.py), crates/orchestrator/
src/{api_public.rs,api_internal.rs,api_admin.rs,registry.rs,scaler.rs,authz.rs,
config.rs,main.rs}, crates/memoryd/src/{api.rs,compact.rs,writer.rs,store.rs},
pod/vihs_pod/{metrics.py,health.py,agent.py,conversation.py,append_buffer.py,
context.py}, scripts/{smoke-test.sh,dev-services.sh,verify.sh},
deploy/docker/compose.dev.yml, ENVIRONMENT.md, COMMANDS.md.

## 6. Files to Change
- Cargo.toml (workspace): add `prometheus` dep (pinned; Decision Log).
- crates/orchestrator/src/metrics.rs (NEW): registry + gauge/counter/hist
  helpers; /metrics text handler; readyz/healthz handlers.
- crates/orchestrator/src/main.rs: mount /healthz /readyz /metrics on public
  router (metrics also on admin router for local scrape); background task
  publishing pod_sessions + scale_events + cold_start from scaler loop.
- crates/orchestrator/src/{api_public.rs,scaler.rs,registry.rs,authz.rs}:
  record resume_total, scale_events_total, cold_start_secs, authz denials.
- crates/memoryd/src/metrics.rs (NEW): registry + handlers.
- crates/memoryd/src/{api.rs,writer.rs,compact.rs,main.rs}: record
  append_latency_ms, compactions_total, memory_blob_tokens,
  epoch_boundary_total, authz denials; mount /metrics.
- pod/vihs_pod/metrics.py: Prometheus text exposition (histogram buckets +
  counters + gauges) on top of existing samples.
- pod/vihs_pod/health.py: serve /metrics alongside /health.
- pod/vihs_pod/agent.py: wire pod metrics into health_fn; cache-ratio poller
  task; model_ver + pod_id labels.
- pod/vihs_pod/{conversation.py,context.py,append_buffer.py}: record
  bargein/abort_flush/premature_endpoint/append_buffer_depth/epoch annotation.
- deploy/observability/{dashboards/*.json,alerts.yml} (NEW).
- deploy/docker/compose.dev.yml: obs profile (prometheus + grafana, dev only).
- scripts/dev-services.sh: `up-obs` / `down-obs` targets.
- scripts/smoke-test.sh: metric-presence assertions after E2E.
- OPERATIONS.md: alert → runbook anchors.
- ENVIRONMENT.md: new env var rows (VIHS_OBS_* ports, VIHS_MOCK_CACHE_RATIO,
  VIHS_MODEL_VER).
- COMMANDS.md: `dev-services.sh up-obs` row.
- OBSERVABILITY.md: name corrections only if implementation reveals mismatch
  (registry stays truthful; additions land here + SPEC-007 in same change).

## 7. Interfaces and Contracts
- GET /metrics on each service, Prometheus text exposition, names EXACTLY per
  registry. Orchestrator: public + admin. memoryd: :8091. pod: local surface.
- GET /healthz → 200 "ok" (liveness). GET /readyz → 200 when deps reachable,
  503 JSON otherwise (orchestrator: memoryd readyz passthrough; memoryd:
  Redis ping, existing).
- Metrics task never touches the media path (SPEC-007 error rule): all
  recording is lock-free counters/gauges or async-isolated poller; a metrics
  panic must not take down the request path (recording is infallible helpers).
- Smoke scrapes after the E2E convo and asserts every required series exists
  with >0 samples.

## 8. Milestones
### M1 — Control-plane metrics + readyz matrix
Goal: orchestrator + memoryd export the control-plane metric set and the
orchestrator gains /healthz + /readyz; all names per registry.
Files to read: crates/orchestrator/src/{api_public.rs,api_admin.rs,scaler.rs,
registry.rs,authz.rs,config.rs,main.rs,lib.rs}, crates/memoryd/src/{api.rs,
compact.rs,writer.rs,main.rs,lib.rs}, OBSERVABILITY.md.
Files to change: Cargo.toml, crates/orchestrator/src/metrics.rs (NEW),
main.rs, api_public.rs, api_admin.rs, scaler.rs, registry.rs, authz.rs,
crates/memoryd/src/metrics.rs (NEW), api.rs, main.rs, compact.rs.
Exact edits expected:
1. Cargo.toml workspace deps: `prometheus = { version = "0.14", default-features = false }`.
2. orchestrator metrics.rs: static registry; helpers `gauge(name, help)`,
   `counter(name, help)`, `hist(name, help, buckets)`; `pub fn render() -> String`;
   `pub fn healthz()`/`readyz(st)` handlers returning axum Responses.
3. main.rs: mount `/metrics` on both public + admin apps; `/healthz` +
   `/readyz` on public app; spawn `metrics_loop` publishing pod_sessions gauge
   per pod_id + scale_events from scaler decisions (or record in scaler_loop
   directly — same task).
4. scaler.rs/registry.rs: record `vihs_scale_events_total{dir}` when a
   Deploy/Terminate/Replace action executes; add `created_at: Instant` to
   PodState; on Booting→Ready transition record `vihs_cold_start_secs`.
5. api_public.rs connect_session: `vihs_resume_total{result=ok|denied|error}`.
6. authz.rs (orchestrator): increment `vihs_authz_denials_total` on denial.
7. memoryd metrics.rs + api.rs: `vihs_append_latency_ms` histogram around
   append_event; `vihs_authz_denials_total`; mount /metrics.
8. memoryd compact.rs: `vihs_compactions_total` + `vihs_memory_blob_tokens`
   + `vihs_epoch_boundary_total` on Compacted::Done.
Validation command: `cargo test -p orchestrator -p memoryd metrics` +
`cargo test -p orchestrator readyz` + manual scrape:
`curl -s localhost:8080/metrics | grep -E '^vihs_'` and
`curl -s localhost:8091/metrics | grep -E '^vihs_'`.
Expected result: unit tests pass (registry render + readyz matrix);
orchestrator /metrics lists vihs_pod_sessions/vihs_scale_events_total/
vihs_cold_start_secs/vihs_resume_total/vihs_authz_denials_total; memoryd
/metrics lists vihs_append_latency_ms/vihs_compactions_total/
vihs_memory_blob_tokens/vihs_epoch_boundary_total/vihs_authz_denials_total;
orchestrator /readyz 200 when memoryd up, 503 when memoryd down.
Recovery: re-run `sh scripts/build.sh` then re-run validation; if memoryd is
down, restart per COMMANDS.md local start before the 503 assertion.

### M2 — Pod metrics incl. cache-ratio poller + epoch annotations
Goal: pod exports the stage/turn/memory metric set as Prometheus text; the
cache-ratio poller and epoch annotation exist and are asserted by a pipeline
flow test.
Files to read: pod/vihs_pod/{metrics.py,health.py,agent.py,conversation.py,
append_buffer.py,context.py,pipeline/*}, tests/e2e or tests/load for
pipeline-flow test conventions (pod tests/ dir).
Files to change: pod/vihs_pod/{metrics.py,health.py,agent.py,conversation.py,
append_buffer.py,context.py}, pod/tests/ (new pipeline-flow test), ENVIRONMENT.md.
Exact edits expected:
1. metrics.py: add Prometheus text render — histogram buckets (fixed buckets
   per stage, e.g. [50,100,200,400,800,1600,3200]), counters
   (bargein, endpoint_premature, epoch_boundary), gauges (append_buffer_depth,
   prefix_cache_hit_ratio, gpu_util); `render_text(labels: dict) -> str`.
2. health.py: process_request handles `/metrics` too.
3. agent.py: hold pod-level Metrics; publish gauge values; spawn cache-ratio
   poller task (env VIHS_MOCK_CACHE_RATIO default 0.95 in mock mode;
   VIHS_VLLM_STATS_URL future real path — mocked now, documented);
   pass model_ver (env VIHS_MODEL_VER default "mock") + pod_id labels.
4. conversation.py: record bargein_total + abort_flush_ms on barge-in abort;
   endpoint_premature_total when user spoke <300 ms after endpoint;
   epoch annotation when a loaded memory blob epoch changes (context.py).
5. append_buffer.py: publish append_buffer_depth gauge after enqueue/drain.
Validation command: `pod/.venv/bin/python -m pytest pod/tests -q -k
"metrics or flow"` and `sh scripts/test-e2e.sh`.
Expected result: new flow test asserts the series exist after one turn
(vihs_stage_first_chunk_ms with stage labels, vihs_e2e_first_audio_ms,
vihs_bargein_total, vihs_append_buffer_depth, vihs_prefix_cache_hit_ratio,
vihs_epoch_boundary_total); E2E still green.
Recovery: rerun the narrow failing test; on import/collect errors, fix the
module that fails to import (check __init__.py wiring).

### M3 — Dashboards + alert rules as code
Goal: deploy/observability/ holds 5 Grafana dashboards + prom alert rules;
`dev-services.sh up-obs` boots prometheus + grafana (dev profile) clean.
Files to read: deploy/docker/compose.dev.yml, scripts/dev-services.sh,
OBSERVABILITY.md Dashboards + Alerts sections.
Files to change: deploy/observability/{dashboards/*.json (5),
alerts.yml}, deploy/docker/compose.dev.yml (obs profile), scripts/
dev-services.sh (up-obs/down-obs), COMMANDS.md, OPERATIONS.md.
Exact edits expected:
1. dashboards/latency.json, turn-taking.json, memory.json, fleet.json,
   resume-security.json — Grafana dashboard JSON with panels wired to the
   registered metric names (one panel per metric family in the registry).
2. alerts.yml — prom rule file: 6 rules per OBSERVABILITY.md Alerts section
   with expr/for/labels/annotations (runbook URL anchor per rule).
3. compose.dev.yml: `obs` profile — prometheus (image prom/prometheus, volume
   mounts alerts.yml + scrape config from deploy/observability/) and grafana
   (image grafana/grafana, provisioning dir, no auth in dev), network_mode
   host for both so they scrape 127.0.0.1:8080/8091/8093.
4. dev-services.sh: `up-obs` starts the obs profile, waits for prometheus
   target up + grafana /api/health 200; `down-obs` removes them.
5. COMMANDS.md: add `up-obs` row. OPERATIONS.md: alert → runbook anchors
   (M4 completes the runbook text; anchors now).
Validation command: `sh scripts/dev-services.sh up-obs` and
`curl -s localhost:9090/api/v1/targets | jq '.data.targets[].health'` +
`curl -s localhost:3000/api/health`.
Expected result: DEV-SERVICES OK (obs); all three scrape targets show "up";
grafana /api/health returns "ok"; dashboards load (grafana API returns the 5
provisioned dashboards).
Recovery: `sh scripts/dev-services.sh down-obs && sh scripts/dev-services.sh
up-obs`; if port conflicts, set VIHS_OBS_PROM_PORT / VIHS_OBS_GRAFANA_PORT.

### M4 — Metric-presence smoke + runbook links
Goal: smoke-test.sh asserts every required series exists after the E2E run;
every alert in OPERATIONS.md names a runbook anchor.
Files to read: scripts/smoke-test.sh, scripts/verify.sh, OPERATIONS.md,
OBSERVABILITY.md.
Files to change: scripts/smoke-test.sh, OPERATIONS.md, OBSERVABILITY.md
(if needed), COMMANDS.md (if new command).
Exact edits expected:
1. smoke-test.sh: after the E2E --smoke run, scrape
   localhost:8080/metrics, localhost:8091/metrics, localhost:8093/metrics;
   assert the full required-series list (from OBSERVABILITY.md) each appears
   with a non-zero sample (grep for `^name` and a count line that is not
   ` 0`); fail with the missing names listed if any are absent.
2. OPERATIONS.md: under Alerts/runbook, one subsection per alert rule naming
   the failure-mode row(s) from the Common failure modes table as the
   runbook anchor.
Validation command: `sh scripts/smoke-test.sh` then full `sh scripts/verify.sh`.
Expected result: SMOKE OK with "METRIC PRESENCE OK"; VERIFY OK end-to-end.
Recovery: if a series is missing, check the owning service's recording site
(M1/M2) before touching the assertion; rerun the narrow scrape first.

## 9. Concrete Steps
Milestone order M1→M4; metrics tasks isolated from media path (SPEC-007
error rule). Log gaps before fixing per EP-007 protocol. Commit per
milestone with `[VIHS][EP-008][M#]` message. Update Progress as work happens.

## 10. Validation and Acceptance
SMOKE OK (with metric presence) + VERIFY OK; acceptance = SPEC-007
acceptance criteria: OBSERVABILITY.md acceptance section satisfied
(all metric names emitted, redaction tests green, dashboards render with live
smoke traffic, alert rules loaded, runbook links from each alert).
Staging soak (cache ratio ≥0.9) is a post-launch item (no staging env in
repo; documented as pending).

## 11. Idempotence and Recovery
All validation commands re-runnable. `up-obs` is idempotent (compose
create/up). Restarting a service rebuilds its metrics registry (counters
reset — acceptable; smoke asserts presence not totals). After interruption:
inspect `git log --oneline -3` for the last M# commit; resume at the next
milestone; re-run that milestone's validation command before continuing.

## 12. Progress
Re-runnable.
- [x] M1 — control-plane metrics + readyz matrix (orchestrator 2 new tests, memoryd 1 new test; live scrape + e2e traffic verified)
- [x] M2 — pod metrics incl. cache-ratio poller + epoch annotations (2 render tests + 1 surface test; pod suite 84; ruff+mypy clean)
- [x] M3 — dashboards + alert rules as code (5 dashboards + 6 rules; promtool valid; up-obs/down-obs with health-wait; targets up incl. live pod probe; grafana /api/health ok)
- [ ] M4

## 13. Surprises & Discoveries
- prometheus 0.14 API differs from 0.13: `TextEncoder::encode_to_string` (not
  `encode`), `HistogramOpts::buckets` (not `Opts::buckets`), `HistogramVec::new`
  takes HistogramOpts.
- `reqwest::Client::builder().timeout()` produced "builder error" — root cause
  was a missing `http://` scheme on memoryd_addr when building the readyz URL
  (reqwest maps an invalid URL to ErrorKind::Builder). MemorydClient already
  normalizes the scheme; readyz_impl now does the same.
- orchestrator already had /healthz + /readyz (Redis-ping via token store)
  inline in main.rs — M1 replaced it with the memoryd passthrough (dependency
  matrix) + /metrics; the EP-007 M3 Redis-down drill still passes because
  memoryd's readyz (which orchestrator now proxies) is the Redis gate.
- cold_start records at the Booting→Ready transition in registry::ready (single
  point) — the initial scaler-loop heuristic was fragile and removed.
- Pod hand-rolled Prometheus render (pure stdlib, per plan): the text format
  allows ONE HELP/TYPE per metric name — a first draft emitted one HELP/TYPE
  per stage label value for `vihs_stage_first_chunk_ms`, which Prometheus
  would reject; grouped into one family with per-stage bucket series.
- Pod endpoint_premature_total has NO live recording site in mock mode: the
  TurnFSM (which emits COUNT_NEAR_MISS) is test-only; the mock pipeline
  bypasses it. The counter renders (0) and the real STT driver records it when
  real stages land (EP-009). Documented, not faked.
- Grafana failed to start on :3000 — the AXIOM egress-plane already owns that
  port on this host. Default moved to 3100 (VIHS_OBS_GRAFANA_PORT); dev
  machines commonly collide on 3000.
- Grafana provisioning scans SUBDIRECTORIES under the provisioning mount
  (datasources/, dashboards/, alerting/) — the first draft put the yml files
  at the mount root and grafana logged "can't read datasources/dashboards".
  Restructured into datasources/ + dashboards/ subdirs.
- Obs stack targets: pod target reads "down" whenever no pod process is
  running (e2e harness tears its pod down). Proven live with a probe pod
  (target -> up, 3 histogram series scraped), then torn down — the target
  state is honest, not masked.

## 14. Decision Log
- prometheus = 0.14 pinned in workspace deps (AGENTS.md §8: necessary for a
  standards-compliant exporter). Rust services use the crate; the pod
  (pure-stdlib) hand-rolls text exposition in M2.
- /metrics on BOTH orchestrator listeners (public :8080 for the M3 obs scrape
  target, admin :8081 for local ops). memoryd /metrics on :8091 (internal-only).
- Orchestrator readyz = memoryd passthrough. Redis is memoryd's dependency; the
  orchestrator's only hard control-plane dependency is memoryd.
- Pod metrics: `render_text` maps pod samples onto registry names
  (lipsync_ff → lipsync_ttff; e2e_first_frame → vihs_e2e_first_audio_ms).
  Cache-ratio gauge reads VIHS_MOCK_CACHE_RATIO (mock) with the real vLLM
  stats poller deferred to EP-009 (documented in code + ENVIRONMENT.md).

## 15. Outcomes & Retrospective
- M1 commit: 419a804 (control-plane metrics + readyz matrix)
- M2 commit: ebe8c97 (pod metrics incl. cache-ratio poller + epoch annotations)
- M3 commit: (pending — fill after commit)
- Remaining gaps: smoke presence + runbooks (M4).

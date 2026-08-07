# OBSERVABILITY.md — VIHS

You cannot tune a real-time pipeline you can't see.

## Logging strategy
Structured JSON lines to stdout everywhere. Required fields: `ts`, `level`,
`service` (orchestrator|memoryd|pod), `event` (snake_case verb),
`session_id?`, `pod_id?`, `turn_id?`, `connection_id?`, `dur_ms?`, `err?`.
Redaction (tested): never log event `text`, audio bytes, bearer tokens,
signed URLs, or raw owner ids (use `owner_hash` = blake3 prefix, 8 hex).

## Metrics (Prometheus; names are the registry — SPEC-007 owns additions)
Latency histograms (all `first_chunk`, labeled `pod_id`, `model_ver`):
`vihs_stage_first_chunk_ms{stage=vad|stt|llm_ttft|clause|tts_ttfa|lipsync_ttff|network}`
plus `vihs_e2e_first_audio_ms` (stop-speaking → first avatar audio; the
product number).
Turn-taking: `vihs_bargein_total`, `vihs_abort_flush_ms` (must sit ≤100 ms),
`vihs_endpoint_premature_total` (user continued <300 ms after we endpointed).
Memory: `vihs_append_latency_ms`, `vihs_append_buffer_depth` (pod-side),
`vihs_prefix_cache_hit_ratio` (validates INV-4 — from vLLM stats),
`vihs_compactions_total`, `vihs_memory_blob_tokens`.
Capacity: `vihs_pod_sessions{pod_id}` vs cap, `vihs_gpu_util`,
`vihs_cold_start_secs` histogram, `vihs_scale_events_total{dir=up|down}`.
Resume: `vihs_resume_total{result=ok|denied|error}`,
`vihs_resume_recall_check` (smoke asserts recalled turn id).

## Traces
Optional in v1: per-turn span tree (turn → stage spans) behind OTLP env; not
a launch gate. Span ids reuse `turn_id`.

## Health/uptime checks
`/healthz` (liveness) + `/readyz` (deps reachable) on both control services;
pod `/health` with per-stage readiness; external uptime probe on orchestrator
public URL.

## Dashboards
1 Latency (stage histograms + e2e p50/p95, budget lines drawn at targets).
2 Turn-taking (barge-in rate, abort-flush, premature endpoints).
3 Memory (append latency, buffer depth, cache hit ratio, blob tokens,
compaction freq). 4 Fleet (pods, fill vs cap, GPU util, cold starts, scale
events). 5 Resume/security (resume results, denied resumes, delete audits).

## Alerts (initial thresholds; tune in EP-008)
- e2e p95 first-audio > 2000 ms for 10 min → page.
- abort_flush p95 > 100 ms for 10 min → page (barge-in is the feel).
- prefix_cache_hit_ratio < 0.9 for 15 min → ticket (INV-4 drift).
- append_buffer_depth > 32 on any pod → page (memoryd trouble).
- resume denied spike (>10/min) → security review.
- pod stale-ping >15 s → auto-handled; alert only if replacements loop 3×.

## SLIs / SLOs (initial)
SLI: e2e first-audio p50; resume success ratio; abort-flush p95.
SLO (post-launch draft): p50 ≤ 1.3 s monthly; resume ≥ 99.9%; flush ≤ 100 ms p95.

## Debugging production issues
Latency: find the stage — histograms are labeled per stage precisely so the
blown budget names its culprit. Memory: `chain-fsck` the session log; compare
Redis index vs log tip. Feel issues with no metric: check endpointing
premature counter and barge-in flush before touching models.

## Observability acceptance criteria (EP-008 exit)
All metric names above emitted; redaction tests green; dashboards render with
live smoke traffic; alert rules loaded; runbook links from each alert.

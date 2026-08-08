# EP-007 M3 — Gap Log (logged BEFORE fixing, per gap-fixing protocol)

Evidence date: 2026-08-08. All gaps found while scoping the two M3 chaos
tests (torn_write_fsck, redis_loss_rebuild) against SPEC-006/SPEC-003.

## GAP-M3-1 [HIGH] — Orchestrator swallows memoryd 409 `integrity_hold` → 503
- Spec: SPEC-003 line 26 (resume row) — `409 integrity_hold` on the PUBLIC
  surface; SPEC-006 "Chain verification failure on load: integrity_hold
  (409), resume denied".
- Evidence: `crates/orchestrator/src/error.rs` `From<MemorydClientError>`
  maps EVERY memoryd error to `OrchError::Upstream` → 503 `upstream`
  (error.rs lines 63-66). memoryd correctly returns 409 `integrity_hold`
  (api.rs `load_session` fscks → IntegrityHold → 409), but the orchestrator
  client (`MemorydClient::load`) surfaces it as `Status(409, body)` and the
  From impl collapses it to 503.
- Impact: the public resume/connect path NEVER returns 409 integrity_hold
  as SPEC-003 promises. The M3 torn_write_fsck chaos test (public surfaces
  only) will observe 503, not the spec'd 409.
- Fix: parse the memoryd error body for `integrity_hold` in the From impl
  and map to `OrchError::IntegrityHold` (409). Add unit test.

## GAP-M3-2 [HIGH] — readyz returns "ok" unconditionally (no Redis check)
- Spec: SPEC-006 "Redis down: memoryd/orchestrator fail readyz".
- Evidence: memoryd api.rs `healthz` = static `"ok"` and `/readyz` routes to
  the SAME handler (api.rs lines 313-315, 326). Orchestrator main.rs lines
  159-160: `/healthz` and `/readyz` both `get(|| async { "ok" })`. No Redis
  dependency is probed by either.
- Impact: after FLUSHALL or Redis outage, readyz still 200. The redis_loss
  chaos test cannot demonstrate the SPEC-006 row honestly.
- Fix: readyz must ping Redis (memoryd: index connection; orchestrator:
  token store connection) and return 503 when unreachable. Keep healthz as
  liveness.

## GAP-M3-3 [HIGH] — rebuild-index does NOT restore owner binding
- Spec: SPEC-006 "on recovery or replacement, `--rebuild-index` path
  documented"; SPEC-005 A1 owner route on every session-scoped call.
- Evidence: `rebuild.rs` calls `index.heal()` which writes ONLY
  tip_hash/last_turn_id/event_count/seq/updated_at (index.rs lines 145-169)
  — NO owner field, NO `owner:{owner}:sessions` zset entry. After FLUSHALL +
  rebuild, the session hash exists but is owner-less.
- Impact: authz (`TokenAuthorizer` user scope) requires
  `snap.owner == principal.owner`; an owner-less record → Load/Delete → 404
  (authz.rs lines 88-92). So after Redis loss + rebuild, the user CANNOT
  load/transcript their own session — the "conversation survives Redis
  loss" recovery path is broken end-to-end. Owner IS recoverable from the
  log (the create note event carries `meta.owner`).
- Fix: rebuild must extract owner from the log's system-note event and
  heal the owner field + owner zset.

## GAP-M3-4 [HIGH] — orchestrator session index not rebuilt on restart
- Spec: SPEC-006 recovery implies the whole path restores; session_index.rs
  documents "rebuildable from memoryd's owner zset on restart (EP-010)".
- Evidence: `SessionIndex` is purely in-memory (session_index.rs) and
  `build_state` (orchestrator lib.rs) seeds tokens but NEVER populates the
  session index from memoryd/Redis. After FLUSHALL, tokens are gone
  (token store lives in Redis) → the recovery drill REQUIRES an orchestrator
  restart to re-seed VIHS_ADMIN_TOKEN + VIHS_POD_TOKEN (lib.rs lines
  101-128). But a restart wipes the in-memory SessionIndex → resume/
  transcript on a surviving session 404 at the orchestrator layer.
- Impact: the honest redis-loss recovery drill (FLUSHALL → restart
  orchestrator to re-seed → rebuild-index → user reconnects) fails at the
  orchestrator even though memoryd has the session.
- Fix: on startup, orchestrator warms SessionIndex from Redis
  (`owner:*:sessions` zsets + `session:{sid}` hashes) — the documented
  rebuild source. Read-only cache warm; no API change.

## Scope note
GAP-M3-1 is required by torn_write_fsck (spec'd 409 on load). GAP-M3-2/3/4
are required by redis_loss_rebuild (readyz row + honest recovery drill).
All four are spec-mandated behavior the chaos suite must demonstrate; fixing
them is M3's product side. The two chaos scripts are the tests.

## GAP-M3-5 [HIGH] — memoryd authz maps ANY token-verify error to 401
- Spec: SPEC-006 retryable split — upstream failures must be retryable;
  a credential must never be reported invalid because the store was down.
- Evidence (found DURING the redis_loss drill, first run): after
  `docker stop/start` of the Redis container, memoryd's long-lived Redis
  connection (built once at startup, no reconnect config) goes stale.
  `TokenAuthorizer::allow` mapped EVERY `TokenError` — including
  `TokenError::Upstream("redis verify: broken pipe")` — to
  `AuthzErr::InvalidToken` → HTTP 401 non-retryable (authz.rs lines
  54-58 pre-fix). The same token/record verified fine (404 authz-OK)
  before the container bounce; a fresh memoryd repro passes. Root cause
  chain: stale pooled connection + error classification swallowing.
- Impact: a transient Redis blip poisons the client's credential state —
  clients cache/invalidate on 401 and never retry. Also masked the true
  recovery need (service restart, SPEC-006 "on recovery or replacement").
- Fix: split the classification in `TokenAuthorizer::allow` —
  `TokenError::Authz(_)` → `AuthzErr::InvalidToken` (401, non-retryable),
  `TokenError::Upstream(m)` → new `AuthzErr::Upstream(m)` mapped to 503
  retryable in error_response. The orchestrator mint path already
  classified upstream correctly (observed 503 retryable during the drill);
  memoryd was the broken half.
- Note: the Redis connection staleness itself (no retry/reconnect config
  on memoryd's ConnectionManager) remains a backlog item; the documented
  recovery is the service restart the drill now performs, and 503 retryable
  makes the failure mode honest until then.

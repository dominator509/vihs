# EP-003 — Data & Persistence (memoryd)

## 1. Purpose / Big Picture
Build the Session Memory Service — the durable brain of continuity and the
ONLY writer of session history (INV-2). After this plan: appends are
hash-chained, idempotent, and crash-safe; `load()` serves a compacted
memory.md + resume cursor; compaction bounds long sessions; Redis is a
rebuildable cache; hard delete and TTL work.

## 2. Scope
`crates/memoryd`: object-store client, Redis index, per-session writer tasks,
HTTP API rows from SPEC-003 (memoryd table), compaction, rebuild-index,
retention sweep, hard delete. Integration tests against dev services.

## 3. Non-goals
No orchestrator logic, no signaling, no pods (EP-004/005 talk TO this).
No auth ENFORCEMENT beyond a pluggable `Authorizer` trait with a permissive
dev impl (real tokens land in EP-006 behind the same trait). No sharding
(ADR-005 v2).

## 4. Context and Orientation
SPEC-002 owns data rules + key registry; SPEC-003 memoryd table owns routes;
ARCHITECTURE §7.3 gives the single-writer reference shape — match it. The
durability order (log line first, Redis second) is the crash-safety crux.

## 5. Files to Read First
SPEC-002, SPEC-003 (memoryd), ARCHITECTURE §7, ADR-003/005, ENVIRONMENT.md
(VIHS_S3_*, VIHS_REDIS_URL, COMPACT_*, SESSION_TTL_DAYS), TESTING.md
integration rules.

## 6. Files to Change
crates/memoryd/src/{main.rs,api.rs,writer.rs,store.rs,index.rs,compact.rs,
sweep.rs,authz.rs,error.rs}, crates/memoryd/tests/{integ_append.rs,
integ_load_render.rs,integ_compact.rs,integ_rebuild.rs,integ_delete_ttl.rs,
integ_crash_order.rs}, Cargo.toml (aws-sdk-s3 or minio-compatible s3 crate,
redis, axum, tokio — pinned; Decision Log records choice).

## 7. Interfaces and Contracts
```rust
// store.rs — the only code that touches sessions/ objects
#[async_trait] pub trait ObjectStore: Send + Sync {
    async fn append_line(&self, sid: &SessionId, sealed: &Value) -> Result<(), StoreErr>;
    async fn read_log(&self, sid: &SessionId) -> Result<ByteStream, StoreErr>;
    async fn put_artifact(&self, sid: &SessionId, name: &str, bytes: &[u8]) -> Result<(), StoreErr>;
    async fn sign_get(&self, key: &str, ttl: Duration) -> Result<Url, StoreErr>;
    async fn sign_put(&self, key: &str, ttl: Duration) -> Result<Url, StoreErr>;
    async fn delete_prefix(&self, sid: &SessionId) -> Result<u64, StoreErr>; // D-9
}
// S3 has no true append. Implementation contract (Decision pre-made, ADR-003
// companion): maintain the log as a single object rewritten via
// read-modify-write is FORBIDDEN (unbounded cost + race). Instead:
//   sessions/{sid}/events/{seq:012}.jsonl  — one small object per append batch
//   read_log = ordered multi-get concatenation; seq from writer state.
// A nightly compact-objects task MAY concatenate closed ranges into
// events.jsonl segments (same bytes, fsck-verified before delete of parts).
// This keeps appends O(1) and preserves SPEC-002 semantics; the LOGICAL log
// is the ordered concatenation. Document in code header + SPEC-002 note.

// writer.rs — per ARCHITECTURE §7.3; plus:
pub struct WriterHandle { pub tx: mpsc::Sender<Append> }
pub struct Registry { /* DashMap<SessionId, WriterHandle>, spawn-on-first-use,
    idle writers retired after 10 min (state safe: tip in index+log) */ }

// api.rs routes exactly per SPEC-003 memoryd table; authz via:
pub trait Authorizer { fn allow(&self, token: &str, sid: &SessionId, verb: Verb) -> Result<Principal, AuthzErr>; }
```

### Hard piece — crash-order recovery (`replay_tip_from_log`)
On writer wake, if Redis tip is missing OR does not match the last object's
last line hash: stream the tail objects backward until a verified line,
recompute tip, heal index. Test `integ_crash_order.rs` kills the process
between store-append and index-advance (injected fault point via
`#[cfg(test)] FailPoint`) and asserts the next append continues the chain
without duplication or tear.

### Hard piece — compaction (compact.rs; SPEC-002 algorithm verbatim)
```rust
pub async fn maybe_compact(sid: &SessionId, deps: &Deps) -> Result<Compacted, CErr> {
    let idx = deps.index.snapshot(sid).await?;
    let tail = deps.cfg.verbatim_tail;                       // COMPACT_VERBATIM_TAIL
    let live = idx.last_turn_id.saturating_sub(idx.compacted_through);
    let blob_tokens = deps.index.blob_tokens(sid).await?;    // cached from last render
    if live <= tail * 2 && blob_tokens <= deps.cfg.token_budget { return Ok(Compacted::NotNeeded) }
    let events = deps.store.read_events_range(sid, idx.summary_ptr.as_deref(),
                                              idx.last_turn_id - tail as u64).await?;
    let new_summary = deps.summarizer.roll(idx.prior_summary_text(), &events).await?; // cheap LLM
    let body = summary_event_body(sid, &new_summary, idx.epoch + 1, covers(&events));
    // Append through the SAME writer path as any event (INV-2; idempotent).
    let res = deps.registry.append(sid, body).await?;
    deps.index.advance_epoch(sid, &res.hash, idx.epoch + 1).await?;
    let mem = render_memory_from_store(deps, sid, tail).await?;   // vihs-core render
    deps.store.put_artifact(sid, "memory.md", mem.as_bytes()).await?;
    Ok(Compacted::Done { epoch: idx.epoch + 1, blob_tokens: estimate(&mem) })
}
```
Trigger points: after every committed turn append (async task, never blocking
the append reply) and the POST /compact safety valve. Summarizer trait has a
deterministic mock for tests (concats bullet lines) — real cheap-LLM impl is
config-gated and NOT required for this plan's acceptance.

## 8. Milestones
M1 Store client + append objects. Validation: `cargo test -p memoryd --test
   integ_append` (dev services up). Expected: append→read_log round-trip,
   idempotent duplicate acked, fsck of concatenation OK.
M2 Writer registry + durability order + crash-order recovery.
   Validation: `--test integ_crash_order`. Expected: healed chain, no tear.
M3 Load/render/artifacts + signed URLs. Validation: `--test
   integ_load_render`. Expected: cursor fields correct; memory.md matches
   vihs-core golden for the generated fixture; signed GET works, expires.
M4 Rebuild-index. Validation: `--test integ_rebuild` — flush Redis, run
   `memoryd --rebuild-index`, index equals pre-flush snapshot.
M5 Compaction. Validation: `--test integ_compact` — 200-turn generated
   session: epoch increments, pre-checkpoint preamble bytes (S0–S2 fetch)
   unchanged within epoch, blob ≤ token budget, supersede rule holds.
M6 Delete + TTL sweep. Validation: `--test integ_delete_ttl` — hard delete
   leaves zero objects/keys, idempotent; injected-clock TTL sweep deletes
   only expired.
M7 API surface + contract tests. Validation: `sh scripts/test-integration.sh`
   INTEGRATION OK (includes memoryd contract rows).

## 9. Concrete Steps
Order above; every integration test namespaces its prefix `test-{uuid}` and
cleans up (TESTING.md). Env read once at boot into a typed Config with
fail-fast validation (ENVIRONMENT.md rules).

## 10. Validation and Acceptance
INTEGRATION OK; acceptance: chain-fsck passes over the concatenated log of
every session the suite created (a sweep step inside test-integration.sh);
append p99 < 50 ms against local MinIO (recorded, informational v1 gate).

## 11. Idempotence and Recovery
All tests re-runnable (namespacing). Writer recovery is the feature under
test — if M2's failpoint test flakes, that is a real bug, not test debt
(anti-fixation: diagnose, don't widen timeouts).

## 12. Progress
- [ ] M1 store  - [ ] M2 writer+crash  - [ ] M3 load/render  - [ ] M4 rebuild
- [ ] M5 compaction  - [ ] M6 delete/ttl  - [ ] M7 api+contract
## 12. Progress
- [x] M1 object store + append + dedup   - [x] M2 crash-order recovery
- [x] M3 load/render + signed URLs        - [x] M4 rebuild-index
- [x] M5 compaction                       - [x] M6 delete + TTL sweep
- [x] M7 API + contract tests

## 13. Surprises & Discoveries
- D-5 dedup keys on the CONTENT hash (body minus chain fields), not the
  sealed hash — a retried write after a dropped reply must dedup regardless
  of how far the tip moved (reproduced by integ_append).
- Crash-order recovery must VERIFY the index tip against the store tail
  (ADR-003: index is a cache, log is truth) — trusting the index blindly
  tore the chain on restart (reproduced by integ_crash_order).
- Summary events are role=system; persona_name must skip kind=Summary or the
  rolling summary text becomes the speaker label (108KB memory blob).
- aws-sdk-s3 1.141: `collect()` returns AggregatedBytes (needs .into_bytes()),
  `is_truncated()` returns Option<bool>, presigned URLs need uri().as_str().
- redis 0.27 needs connection-manager feature for aio::ConnectionManager;
  get_multiplexed_async_connection is the non-deprecated test path.

## 14. Decision Log
- S3 client: aws-sdk-s3 1.141 (pinned by lockfile), presigning via
  PresigningConfig; endpoint_url override for MinIO dev.
- Redis client: redis 0.27 with tokio-comp + connection-manager.
- New module rebuild.rs (lib) so rebuild-index is testable; main.rs calls it.
- One small object per append (events/{seq:012}.jsonl) — O(1) append on S3.
- 404 (not 403) for unknown sessions — no ID oracle (SPEC-005).

## 15. Outcomes & Retrospective
## 15. Outcomes & Retrospective

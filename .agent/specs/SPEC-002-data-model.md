# SPEC-002 — Data Model, Storage Keys, Compaction, Retention

Status: accepted · Owner: djw · Roadmap phases: 1, 2 · Linked ExecPlans: EP-002, EP-003

## User-visible goal
Nothing the user said or heard is ever lost, misordered, or silently altered;
long conversations stay fast and cheap; deletion really deletes.

## Non-goals
Relational storage; cross-session analytics warehouse; full-text search.

## Terms
Event; chain; tip; epoch; summary event; verbatim tail; rendered artifact.

## Entities & relationships
Session 1—N Event (ordered, hash-chained). Session 1—1 index hash (Redis).
Session 1—N audio objects (per assistant turn, optional). Session 1—2 derived
artifacts (transcript.md, memory.md). Owner 1—N Sessions.

## D-rules (normative data rules)
- D-1 Event log is append-only; historical events are never rewritten (a
  rewrite breaks the chain — that is the feature, not a bug).
- D-2 Canonical encoding: one canonical-JSON object per line (keys sorted at
  all depths, UTF-8, no insignificant whitespace); `hash` =
  `blake3(canonical_bytes(event minus hash))`; `prev_hash` links to prior
  event; first event's `prev_hash` = `"blake3:genesis"`.
- D-3 `turn_id` is non-decreasing; multiple events may share a turn (e.g. an
  utterance + a note).
- D-4 Event kinds: `utterance | tool_call | tool_result | note | summary`.
  Roles: `user | assistant | system | tool`. A `summary` event is always
  role `system` and carries `meta.covers = {from_turn, to_turn}` and
  `meta.epoch`.
- D-5 Idempotent append: an append whose computed hash equals an
  already-committed hash is acknowledged as Duplicate and not re-written
  (dedup window ≥ last 64 hashes; full-log check on tip mismatch).
- D-6 Schema evolution: bump `v`; readers tolerate `v` and `v±1`; unknown
  fields preserved on read-modify-never (we never modify), rejected on append
  above current writer `v`.
- D-7 No floats in hashed fields: `asr_conf` is stored as integer basis
  points (9400 = 0.94) inside `meta`; all durations integer ms; the
  canonicalizer REJECTS floats to make this unrepresentable.
- D-8 Derived artifacts (`transcript.md`, `memory.md`) are pure functions of
  the log (INV-5): `render(events) → bytes` is deterministic — same events ⇒
  same bytes, byte-exact, locked by golden tests.
- D-9 Hard delete removes, in order: rendered artifacts, audio objects, the
  event log object, the Redis index; idempotent (re-delete is a no-op OK);
  emits an audit log line with ids only.
- D-10 Retention: sessions idle past `SESSION_TTL_DAYS` are hard-deleted by
  the memoryd sweep (same code path as D-9).

## Storage key registry (canonical; agents must not invent keys)
Object store (bucket `VIHS_S3_BUCKET`):
```
sessions/{session_id}/events.jsonl        # the truth (D-1..D-7)
sessions/{session_id}/turn-{turn_id}.opus # optional rendered audio
sessions/{session_id}/transcript.md       # derived (D-8)
sessions/{session_id}/memory.md           # derived (D-8), injection blob
```
Redis:
```
session:{session_id}   HASH  fields: owner, created_at, last_turn_id,
                             event_count, tip_hash, epoch, summary_ptr,
                             live_pod, content_hash, updated_at
owner:{owner_id}:sessions  ZSET  member=session_id score=updated_at
pod:{pod_id}           HASH  fields: addr, cap, fill, last_ping, state
pods:active            SET   pod ids
scale:queue            LIST  queued connection requests (orchestrator)
```

## memory.md rendered shape (normative example)
```markdown
# Session 7f3a — Continuity Memory
Persona: "Aria" · Started 2026-07-05 · Turns: 42 · Compacted through turn 30

## Summary (turns 1–30)
> User is architecting a self-hosted avatar suite. Settled on preemptive
> autoscaling at 4/5 fill, warm-pool floor of 1, and RunPod 4090 pods.
> Open thread: whether to disaggregate the LLM from the renderer.

## Recent turns (31–42, verbatim)
**User** (14:02): Where did we land on the pod autoscaler?
**Aria** (14:02): You chose preemptive scaling at 4 of 5 concurrent…
```
transcript.md renders ALL turns verbatim with an interruption marker
(`⟪interrupted⟫`) where `meta.interrupted` is true.

## Compaction algorithm (memoryd, async at turn boundaries only)
Trigger: `live_turns_since_epoch > COMPACT_VERBATIM_TAIL * 2` OR rendered
memory.md token estimate > `COMPACT_TOKEN_BUDGET`. Never mid-turn.
Steps (all crash-safe because output is one appended event + re-render):
1. Read events from `summary_ptr` (or genesis) through `last_turn -
   COMPACT_VERBATIM_TAIL`.
2. Cheap-LLM pass produces an updated rolling summary text (prompt template
   is a versioned fixture; includes prior summary so it's rolling, not
   from-scratch).
3. Append ONE `summary` event (D-4) with `meta.epoch = epoch+1`,
   `meta.covers`, chained like any event. This freezes the summary: epoch N's
   preamble bytes never change again (INV-4).
4. Update Redis `epoch`, `summary_ptr` = that event's hash; re-render
   memory.md (frozen summary + verbatim tail) and store it.
Failure at any step: re-run is safe (step 3's idempotent hash dedup makes a
retried identical summary a no-op; a differing regenerated summary appends as
the next event and simply supersedes — readers take the LAST summary event).
Effect: bounded tokens ⇒ bounded TTFT ⇒ bounded cost; the prefix cache
cold-misses once per epoch instead of every turn.

## Rebuild-index (`memoryd --rebuild-index`)
Streams every `sessions/*/events.jsonl`, fscks the chain, recomputes the
Redis hash fields from the log. Redis is disposable (ADR-003).

## Inputs / outputs / error states
Append input: event body minus `prev_hash`/`hash` (writer fills), validated:
schema, size ≤64 KiB, `session_id` matches auth binding, `audio_ref` prefix
matches session. Errors: `Rejected(reason)` non-retryable (schema, float,
auth), `Retryable` (store I/O). Load output: memory.md bytes + `{tip_hash,
last_turn_id, epoch}` cursor. Chain failure on load ⇒ session flagged
`integrity_hold`, resume denied, SEV1 (OPERATIONS).

## Security rules
Only memoryd holds store write credentials. Pods receive signed GET (memory)
/ PUT (audio) URLs ≤15 min. Appends authenticated with pod tokens bound to
one session (SPEC-005).

## Observability rules
append latency, compaction count, blob tokens, rebuild duration (SPEC-007).

## Migrations
None; D-6 governs evolution. A CBOR re-encode tool is the only anticipated
bulk transform (ADR-004) and would write NEW objects, not rewrite.

## Required tests
Canonicalizer property tests (sorted keys, float rejection, stability);
fsck mutation tests (flip any byte ⇒ detected); idempotent-append test
(same body twice ⇒ one line); durability-order crash test (kill between store
write and index write ⇒ recovery heals); golden render tests (byte-exact);
compaction: epoch freeze test (pre-checkpoint preamble bytes unchanged),
rolling-summary supersede test; rebuild-index equivalence test; hard-delete
leaves zero keys/objects; TTL sweep test with injected clock.

## Acceptance criteria
`chain-fsck` passes on every log the integration suite produces, including
chaos runs; golden renders byte-stable across platforms in CI; a 200-turn
generated session compacts to ≤ COMPACT_TOKEN_BUDGET tokens with all
invariant tests green.

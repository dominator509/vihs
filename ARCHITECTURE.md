# ARCHITECTURE.md — VIHS

## Purpose
Define the concrete boundaries, flows, and invariants of VIHS so any coding
agent can place new code correctly and never violate the durability or latency
contracts. Every rule here is a repository constraint, not a philosophy.

## 1. System overview
VIHS ingests live user audio over WebRTC, produces an autonomous spoken
response, renders synchronized facial motion onto a base asset, and streams it
back — while an orchestration layer scales GPU pods on demand and a memory
service makes every conversation durable and resumable.

Three separations of concern carry the whole design:

| Separation | Ephemeral | Durable |
|---|---|---|
| Identity | connection, pod assignment | `session_id` (the conversation) |
| Compute | GPU pods (disposable) | model weights + assets (network volume) |
| State | in-pod runtime buffers | event log + rendered memory (object store) |

Pods can die, users can reconnect to a different pod, and the conversation is
untouched.

## 2. Repository map (intended; enforced from EP-001)
```
/
  Cargo.toml                  # workspace: vihs-core, vihs-auth, memoryd, orchestrator, mcpd
  crates/
    vihs-core/                # shared types, event schema, hash chain,
                              # deterministic render, chain-fsck bin
    vihs-auth/                # shared token store (mint/verify/revoke),
                              # Scope/Principal, argon2id+pepper (SPEC-005)
    memoryd/                  # Session Memory Service (single writer)
    orchestrator/             # router, autoscaler, signaling, auth gateway
    mcpd/                     # MCP server (ADR-011): thin adapter over the
                              # same orchestrator ops; vihs_* tools
  pod/                        # Python 3.11 pod agent
    vihs_pod/
      agent.py                # entrypoint, health, signaling client
      pipeline/               # vad.py stt.py llm.py tts.py lipsync.py mux.py
      turn.py                 # endpointing FSM + barge-in ABORT bus
      context.py              # prefix-stable context assembly (INV-4)
      memory_client.py        # append/fetch against memoryd
      mocks/                  # --mock-gpu stage implementations for CI
    tests/
    pyproject.toml  requirements.lock
  client/                     # static HTML/JS WebRTC client (no npm)
    index.html  app.js  webrtc.js  session.js  styles.css  idle-loop.mp4(ref)
  deploy/
    docker/pod.Dockerfile  docker/compose.dev.yml
    runpod/                   # provider templates + volume layout doc
  scripts/                    # POSIX sh, see COMMANDS.md
  .agent/                     # plans, specs, checklists, prompts, templates
```

## 3. Layer responsibilities and dependency rules
- `vihs-core` (Layer 0): event schema, canonical encoding, blake3 hash chain,
  markdown render, ID types, redaction helpers (owner_hash blake3 8-hex +
  scrub_log_line for the log boundary — EP-006 M4). Depends on nothing else
  in the workspace.
- `vihs-auth` (Layer 0): the shared token store — mint/seed/verify/revoke,
  Scope/Principal, argon2id+pepper params (SPEC-005). Added in EP-006 M3 so
  memoryd verifies tokens minted by orchestrator with ONE implementation and
  ONE pepper (VIHS_TOKEN_PEPPER); duplicating the crypto per service would
  let the peppers drift and break cross-service verify. Imports nothing from
  the service crates (vihs-core only, transitively none).
- `memoryd` (Layer 1): owns the append-only event log; the only writer (INV-2).
  Imports `vihs-core` and `vihs-auth`. Must not import `orchestrator`.
- `orchestrator` (Layer 1): sessions, routing, autoscaling, signaling, auth.
  Imports `vihs-core` and `vihs-auth`. Must not import `memoryd` (talks to it
  over HTTP only).
- `pod/` (data plane): stateless pipeline. Talks to orchestrator (signaling,
  health) and memoryd (append, fetch memory.md) over the network only. Holds
  no durable state (INV-3).
- `client/`: talks only to orchestrator (signaling/API) and to its assigned pod
  (media). Never talks to memoryd or Redis directly.

Import rules (concrete): `memoryd` and `orchestrator` may import `vihs-core`;
`vihs-core` must not import either. No crate imports pod code; the network is
the only bridge between control plane and data plane. The client bundles no
server code.

## 4. Identity model (four IDs, four lifetimes)
| ID | Scope | Lifetime | Purpose |
|---|---|---|---|
| `session_id` | one conversation | durable | resume token; crypto-random UUIDv4 |
| `connection_id` | one WebRTC connection | ephemeral | one transport attempt |
| `pod_id` | one GPU pod | ephemeral | which node serves right now |
| `turn_id` | one exchange | monotonic per session | ordering + resume cursor |

A session is a sequence of connections; each connection runs on one pod; the
session's memory is independent of both. `session_id` is unguessable but is
NOT an auth token: resuming requires the owner's authenticated credential
(SPEC-005). A leaked ID must never replay a transcript.

## 5. Core invariants (the contract; each has an owning test)
- INV-1 Log what was rendered, not what was generated. On barge-in, the
  transcript records only audio the user actually heard, `interrupted: true`.
  Test: `pod/tests/test_bargein_inv1.py`.
- INV-2 Single writer per session. All appends funnel through memoryd's
  per-session writer task; no interleaving, no torn chains.
  Test: `memoryd` concurrent-append test + `chain-fsck`.
- INV-3 Memory is pod-independent. No conversational state lives only in a
  pod. The last committed event is always the resume point.
  Test: E2E kill-pod-mid-turn drill (EP-010).
- INV-4 Prefix stability. The `[system | persona | memory.md]` preamble is
  byte-stable within a session between compaction checkpoints so the LLM
  prefix cache stays hot. Test: `pod/tests/test_prefix_stability.py` asserts
  identical preamble bytes across turns in one epoch.
- INV-5 Derived artifacts are a cache, never truth. `transcript.md` and
  `memory.md` are pure functions of the event log; regenerable at any time.
  Test: `vihs-core` render determinism test (same events ⇒ same bytes).

Forbidden architecture moves: a second event-log writer path; pod-local
session persistence; mutating a frozen summary; renderer output that isn't a
pure function of events; auth decisions keyed on `session_id` alone; blocking
the media path on memoryd availability (appends buffer + retry, see §9).

## 6. Runtime flow — the streamed pipeline (inside a pod)
Sequential full-response pipelines land 4–8 s and feel broken. Every stage is
streamed and pipelined at clause granularity; first-frame latency ≈ the sum of
each stage's FIRST-CHUNK latency. The pipelining is the product.

```
        ┌──────────── barge-in ABORT bus ────────────┐
        │  (VAD fires during playback → flush all)   │
        ▼                                            │
[VAD + endpointing] → [streaming STT] → [LLM] → [TTS] → [lip-sync] → [mux] → WebRTC out
        ▲ user audio in     (clause-by-clause; each stage starts on first chunk)
```

1. Ears: Silero VAD gates speech; the endpointer decides the user finished
   (semantic endpointing trades +100–300 ms against premature cut-offs).
   Streaming STT transcribes chunks as they arrive so text is ready the
   instant endpointing fires.
2. Brain: context ordered `[stable prefix | memory preamble | live turns]`
   (INV-4); generation streams token-by-token.
3. Voice: the moment a completable clause exists, hand it to streaming TTS —
   never wait for the full response.
4. Face: first TTS chunk drives the lip-sync renderer immediately; a
   micro-motion driver injects idle nods/blinks so the puppet never freezes,
   including while listening.
5. Stream: frames + audio muxed (GStreamer) onto the outbound WebRTC track.
6. Barge-in: VAD during playback raises ABORT → cancel LLM, flush TTS queue,
   stop renderer, clear outbound buffer, neutral listening pose, all within
   ~100 ms perceived. Per INV-1, only played audio is committed.

### Latency budget (first-chunk, critical path)
| Stage | Target | Lever |
|---|---|---|
| Endpointing/VAD | 100–300 ms | semantic vs silence-threshold trade |
| STT final flush | 50–150 ms | streaming ASR, partials pre-computed |
| LLM TTFT | 150–400 ms | prefix cache (persona+memory), model size |
| First completable clause | +100–250 ms | clause-boundary chunking |
| TTS TTFA | 100–300 ms p50 / ≤800 ms p95 | Piper streaming. EP-010 M2 measured (Lexi local + bounded ONNX threads): p50 68–152 ms; p95 tail to ~600 ms is the longest single clause (content-length-bound), not contention — budget amended 2026-08-11 per operator decision. |
| Lip-sync first frame | 100–400 ms | usually THE bottleneck; lighter model |
| Network + jitter buffer | 50–150 ms | TURN only when needed |
Total well-pipelined: ~0.7–1.3 s, inside the 1.5 s target.

Prefix-cache tie-in: because the preamble is byte-stable within an epoch
(INV-4), vLLM serves it from cache and TTFT is dominated by the new turn's
tokens — the memory blob is paid essentially once per compaction epoch.

## 7. Data flow and persistence boundaries
| Tier | Store | Contents | Truth? |
|---|---|---|---|
| Event log | Object store `sessions/{session_id}/events.jsonl` | append-only typed events, blake3 hash-chained | ✅ source of truth |
| Session index | Redis hash `session:{id}` | `last_turn_id`, `event_count`, `owner`, `summary_ptr`, `live_pod`, `content_hash`, timestamps | hot cache |
| Rendered artifacts | Object store `sessions/{id}/transcript.md`, `memory.md` | derived (INV-5) | ❌ cache |
Only memoryd touches the event log and rendered artifacts. Only orchestrator
and memoryd touch Redis. Pods read `memory.md` via short-lived signed URLs.

### 7.1 Event schema (canonical; SPEC-002 is the registry)
```json
{
  "v": 1,
  "session_id": "7f3a1c9e-…",
  "turn_id": 42,
  "ts": "2026-07-07T18:22:31.482Z",
  "role": "user | assistant | system | tool",
  "kind": "utterance | tool_call | tool_result | note | summary",
  "text": "…what was said / heard…",
  "audio_ref": "s3://vihs-sessions/sessions/7f3a…/turn-42.opus",
  "meta": { "asr_conf": 0.94, "interrupted": false, "latency_ms": 812,
            "voice": "aria-v2", "tokens": 128 },
  "prev_hash": "blake3:9a2f…",
  "hash": "blake3:4c71…"
}
```
The hash chain buys three things for one field: tamper-evidence (any edit
breaks the chain), an exact resume cursor ("resume from event hash = X" is
unambiguous across crashes), and idempotent append (a retried write is a no-op
if the hash already exists — a mid-turn pod death cannot double-log).

### 7.2 Hard piece #1 — canonical bytes + hash chain (Rust, `vihs-core`)
The chain is only sound if hashing runs over CANONICAL bytes: keys sorted,
no insignificant whitespace, `hash` field excluded from its own preimage,
floats forbidden in hashed fields (asr_conf stored as integer basis points in
meta before canonicalization — see SPEC-002 rule D-7). Reference
implementation the coding agent must match:

```rust
// crates/vihs-core/src/chain.rs
use serde_json::{Map, Value};

pub const GENESIS: &str = "blake3:genesis";

/// Canonical JSON: object keys sorted lexicographically at every depth,
/// UTF-8, no whitespace. `hash` is stripped before encoding so the hash
/// covers everything else INCLUDING prev_hash (that's what makes it a chain).
pub fn canonical_bytes(event: &Value) -> Result<Vec<u8>, ChainError> {
    fn canon(v: &Value, out: &mut Vec<u8>) -> Result<(), ChainError> {
        match v {
            Value::Object(m) => {
                out.push(b'{');
                let mut keys: Vec<&String> = m.keys().collect();
                keys.sort_unstable();
                for (i, k) in keys.iter().enumerate() {
                    if i > 0 { out.push(b','); }
                    serde_json::to_writer(&mut *out, k).map_err(ChainError::Encode)?;
                    out.push(b':');
                    canon(&m[k.as_str()], out)?;
                }
                out.push(b'}');
            }
            Value::Number(n) if n.is_f64() => return Err(ChainError::FloatInHashedField),
            other => serde_json::to_writer(&mut *out, other).map_err(ChainError::Encode)?,
        }
        Ok(())
    }
    let mut stripped: Map<String, Value> =
        event.as_object().ok_or(ChainError::NotObject)?.clone();
    stripped.remove("hash");
    let mut out = Vec::with_capacity(256);
    canon(&Value::Object(stripped), &mut out)?;
    Ok(out)
}

pub fn compute_hash(event: &Value) -> Result<String, ChainError> {
    Ok(format!("blake3:{}", blake3::hash(&canonical_bytes(event)?).to_hex()))
}

/// Verify a full log. Returns (event_count, tip_hash).
/// Rules: event[0].prev_hash == GENESIS; event[i].prev_hash == event[i-1].hash;
/// each event.hash recomputes; turn_id non-decreasing.
pub fn fsck<'a, I: Iterator<Item = &'a Value>>(events: I)
    -> Result<(u64, String), ChainError>
{
    let (mut n, mut tip, mut last_turn) = (0u64, GENESIS.to_string(), 0u64);
    for ev in events {
        let prev = ev["prev_hash"].as_str().ok_or(ChainError::MissingField("prev_hash"))?;
        if prev != tip { return Err(ChainError::Torn { at: n }); }
        let claimed = ev["hash"].as_str().ok_or(ChainError::MissingField("hash"))?;
        if compute_hash(ev)? != claimed { return Err(ChainError::BadHash { at: n }); }
        let t = ev["turn_id"].as_u64().ok_or(ChainError::MissingField("turn_id"))?;
        if t < last_turn { return Err(ChainError::TurnRegression { at: n }); }
        last_turn = t; tip = claimed.to_string(); n += 1;
    }
    Ok((n, tip))
}
```

### 7.3 Hard piece #2 — single-writer idempotent append (memoryd)
INV-2 is enforced structurally, not by locks scattered around: one tokio task
per hot session owns the log tail; all appends are messages to it.

```rust
// crates/memoryd/src/writer.rs (shape the implementation must keep)
pub enum Append { Event { body: Value, reply: oneshot::Sender<AppendResult> } }

pub async fn session_writer(sid: SessionId, store: ObjectStore, idx: RedisIndex,
                            mut rx: mpsc::Receiver<Append>) {
    // Recover tail state once, on first message after wake.
    let mut tip = idx.tip_hash(&sid).await
        .unwrap_or_else(|| replay_tip_from_log(&store, &sid)); // crash-safe
    let mut seen_tail: LruSet<String> = idx.recent_hashes(&sid, 64).await;

    while let Some(Append::Event { mut body, reply }) = rx.recv().await {
        body["prev_hash"] = Value::from(tip.clone());
        let h = match compute_hash(&body) { Ok(h) => h,
            Err(e) => { let _ = reply.send(AppendResult::Rejected(e.into())); continue } };
        if seen_tail.contains(&h) {                    // idempotent retry: no-op
            let _ = reply.send(AppendResult::Duplicate { hash: h }); continue;
        }
        body["hash"] = Value::from(h.clone());
        // Durability order matters: object store line FIRST, Redis index SECOND.
        // A crash between the two is healed by replay_tip_from_log on next wake;
        // the index is a cache (INV-5 spirit), the log is truth.
        if let Err(e) = store.append_line(&sid, &body).await {
            let _ = reply.send(AppendResult::Retryable(e.into())); continue;
        }
        idx.advance(&sid, &h, body["turn_id"].as_u64().unwrap()).await.ok();
        seen_tail.insert(h.clone()); tip = h;
        let _ = reply.send(AppendResult::Committed { hash: tip.clone() });
    }
    // channel closed ⇒ session cold; task ends; tip lives in log + index.
}
```
Router rule: exactly one live `session_writer` per session per memoryd
process; memoryd is a single process in v1 (ADR-005 documents the sharded-v2
path: consistent-hash sessions across memoryd replicas, still one writer per
session).

## 8. Request/command flows
Fresh session: client authenticates → orchestrator mints `session_id`, records
owner in Redis → router picks least-loaded healthy pod under cap (or triggers
deploy, queues user, client shows idle loop) → WebRTC SDP/ICE via orchestrator
signaling → pipeline loop; each committed turn appended via memoryd.
Teardown: stream closes → pod frees slot → orchestrator decrements
concurrency → empty pod starts cooldown → terminated if still empty at expiry
and above warm-pool floor.

Resume: `resume(session_id, auth)` → orchestrator authorizes OWNER →
memoryd `load()` renders `memory.md` (frozen summary + verbatim tail) +
cursor → orchestrator assigns pod with short-lived signed memory URL → pod
fetches, builds `[prefix | memory | live]`, WebRTC connects, conversation
continues at turn N+1. A resumed session is indistinguishable from a live one
to the pipeline.

## 9. State management rules
- Pod state = runtime buffers only; anything worth keeping is an event.
- Append availability: the pod buffers committed-turn events in a bounded
  local queue (RAM, max 64) with retry/backoff to memoryd; the media path
  never blocks on memoryd. If the buffer fills, the pod health-degrades and
  the orchestrator drains it (STOP-equivalent for that pod, not the session).
- Redis is rebuildable from object store; a startup `--rebuild-index` path
  must exist in memoryd (EP-003 M4).

## 10. Security boundaries (detail in SECURITY.md / SPEC-005)
Auth gateway at orchestrator: opaque bearer → owner id. Ownership check on
every session-scoped call. Pods hold only short-lived signed URLs + a per-
assignment pod token, never standing store credentials. Encryption at rest on
the object store. Hard delete removes events, artifacts, audio, index.

## 11. Validation and error-handling boundaries
Every network edge validates before use: signaling messages (schema +
size caps), memoryd append (schema, chain fields, per-session auth),
client API (auth, ownership). Error taxonomy and retry classes in SPEC-006.

## 12. Observability boundaries
Per-stage first-chunk histograms tagged `pod_id` + model version; barge-in
rate and abort-flush time; prefix-cache hit rate (validates INV-4);
compaction frequency + memory-blob tokens; per-pod concurrency vs GPU util;
cold-start distribution. Details + metric names in SPEC-007/OBSERVABILITY.md.

## 13. Scaling rules
Derive the cap, don't assume it:
```
sessions_per_gpu ≈ min( VRAM_available / VRAM_per_session,
                        gpu_ms_per_second / (render_ms_per_frame × target_fps) )
```
"5 per 4090" is plausible for lightweight lip-sync; heavier talking-head
models land 1–2. The cap is a LOAD-TESTED figure (EP-007 M3), re-measured on
any model change, enforced hard by the router. Warm-pool floor ≥ 1. Preemptive
scale at 4/5 fill — never let the 5th user queue behind a cold start. Health
ping every 5 s; on crash: terminate, replace, reconnect affected users via the
resume path (they lose at most the in-flight turn).

Monolithic pod first (lowest latency, simplest). If LLM or TTS saturates well
before the renderer, peel that stage into a pooled service — the identity and
memory model is unchanged either way (ADR-009 records the trigger metrics).

## 14. How to add a new feature
1. Write/extend the owning SPEC (behavior-first). 2. Add/extend an ExecPlan
via the template. 3. Place code per the layer map (§2/§3). 4. Add the required
tests per TESTING.md. 5. Update COMMANDS/ENVIRONMENT if commands/vars change.
6. ADR if a boundary or invariant is touched (invariants need extraordinary
justification).

## 15. How to add a dependency / modify schema / add an integration
Dependency: AGENTS.md §8. Schema: bump event `v`, keep readers tolerant of
older `v`, never rewrite historical events (SPEC-002 evolution rules).
Integration (new TTS/STT/renderer/provider): implement the existing stage
Protocol (pod) or `PodProvider` trait (orchestrator); every §9-menu row is a
swap behind these seams, touching no invariant.

## 16. Architecture review checklist
[ ] No new writer path to the event log. [ ] No pod-durable state. [ ] Preamble
byte-stability preserved across the change. [ ] Derived artifacts still pure.
[ ] Import rules hold (`cargo deny` layer check + grep gate in lint.sh).
[ ] New edges validate input. [ ] New stage instrumented with first-chunk
histogram. [ ] Cap/pooling changes backed by load-test numbers.

# DECISIONS.md — Architecture Decision Log (VIHS)

Rules: any decision that touches a boundary, invariant, dependency of
architectural weight, or §9 technology-menu row gets an ADR using
`.agent/templates/adr-template.md`. Ordinary implementation choices go in the
ExecPlan Decision Log instead. Status ∈ {proposed, accepted, superseded}.

## Decision table / ADR index
| ADR | Title | Status | Date | Owner |
|---|---|---|---|---|
| 001 | Rust control plane, Python pod agent | accepted | 2026-07-17 | djw |
| 002 | JSONL + canonical-JSON + blake3 hash chain for event log | accepted | 2026-07-17 | djw |
| 003 | Redis hot index rebuildable from object-store truth | accepted | 2026-07-17 | djw |
| 004 | CBOR upgrade path deferred | accepted | 2026-07-17 | djw |
| 005 | memoryd single process v1; consistent-hash shard v2 | accepted | 2026-07-17 | djw |
| 006 | Opaque bearer tokens v1; OIDC v2 | accepted | 2026-07-17 | djw |
| 007 | aiortc in-pod WebRTC; LiveKit as fallback swap | accepted | 2026-07-17 | djw |
| 008 | Encryption at rest via store SSE, not app-layer envelope | accepted | 2026-07-17 | djw |
| 009 | Monolithic pod first; disaggregate on utilization trigger | accepted | 2026-07-17 | djw |
| 010 | Cap default 2/pod until load-test derives real figure | accepted | 2026-07-17 | djw |

## ADR-001 Rust control plane, Python pod agent
Context: durability invariants (INV-2/3/4/5) live in the control plane; the
GPU pipeline must use Python-native ML runtimes (vLLM client, faster-whisper,
Silero, aiortc, TTS/lip-sync).
Decision: Rust (axum/tokio) for orchestrator+memoryd+core; Python 3.11 for the
pod agent; network-only bridge.
Alternatives: all-Python (weaker guarantees where they matter most); all-Rust
(fighting the ML ecosystem, months of binding work); Go control plane (no
operator advantage).
Consequences: two toolchains; CI runs both; mock GPU stages keep CI CPU-only.

## ADR-002 JSONL + canonical JSON + blake3
Context: §6.1 names JSONL as pragmatic default; the chain requires
deterministic bytes.
Decision: one canonical-JSON object per line; blake3 over canonical bytes with
`hash` stripped; floats forbidden in hashed fields.
Alternatives: length-prefixed CBOR (deterministic by construction, less
greppable — deferred, ADR-004); protobuf (schema tooling overhead).
Consequences: cheap tooling (`jq`, grep); canonicalizer is load-bearing and
gets property tests (EP-002 M3).

## ADR-003 Redis index rebuildable
Decision: Redis `session:{id}` is a cache; memoryd ships `--rebuild-index`
that replays object-store logs. Durability order on append: log line first,
index second. Consequences: Redis loss = degraded latency, never data loss.

## ADR-004 CBOR deferred
Decision: keep JSONL until either log-size or parse-cost metrics (SPEC-007)
cross documented thresholds; the schema maps 1:1 so a re-encode tool is a
bounded task. Status: accepted (deferral).

## ADR-005 memoryd single process v1
Context: INV-2 requires one logical writer per session. Decision: one memoryd
process, per-session tokio writer tasks. v2 trigger: sustained append p99 >
50 ms or >5k hot sessions → consistent-hash sessions across replicas (writer
uniqueness preserved per shard). Consequences: memoryd is a SPOF in v1;
mitigated by pod-side buffered retry (ARCHITECTURE §9) and fast restart.

## ADR-006 Opaque bearer tokens v1
Decision: operator-issued tokens, argon2id-hashed at rest, per-owner; resume
requires owner match. OIDC/OAuth is a v2 swap behind the same
`authorize(token) -> OwnerId` seam. Consequences: no third-party IdP
dependency for self-hosters.

## ADR-007 aiortc in-pod
Decision: media terminates in the pod agent (aiortc) — zero extra hops,
simplest latency path. Fallback: LiveKit (self-hosted) if aiortc cannot hold
target fps under the derived cap; swap sits behind `pipeline/mux.py` +
signaling adapter. Trigger metric: mux/network stage p95 over budget in EP-007
load test.

## ADR-008 Store-level encryption at rest
Decision: MinIO KMS / S3 SSE + FDE on control-plane hosts satisfies §6.5 for
v1 threat model (SECURITY.md). App-layer envelope encryption documented as the
upgrade if the operator must defend against a hostile storage admin.

## ADR-009 Monolithic pod first
Decision: STT+LLM+TTS+render co-located per pod (lowest latency, no stranded
complexity). Disaggregation trigger: LLM or TTS GPU-utilization saturates at
<70% renderer utilization in capacity tests → peel that stage into a pooled
service. Identity/memory model unchanged either way.

## ADR-010 Cap default 2
Decision: `POD_MAX_SESSIONS=2` until `loadtest-capacity.sh` derives the real
figure per model set; router enforces hard. Re-derive on any model change.

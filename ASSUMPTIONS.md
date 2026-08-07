# ASSUMPTIONS

Assumptions made during blueprint generation. Verify before or during EP-000/EP-001.
Blocking = implementation cannot proceed safely until confirmed.

| # | Assumption | Reason | Risk if wrong | How to verify | Blocks? |
|---|---|---|---|---|---|
| A-01 | Greenfield repository | Stated in inputs | Discovery plan wasted effort only | `git log --oneline` empty / `ls` empty | No |
| A-02 | Control plane in Rust (axum 0.7 + tokio), pod agent in Python 3.11 | Operator is Rust-first; ML inference runtimes (vLLM, faster-whisper, aiortc, TTS, lip-sync) are Python-native. Splitting keeps Rust everywhere the durability invariants live | Team cannot maintain one of the languages | Confirm toolchains in preflight (`cargo --version`, `python3.11 --version`) | No |
| A-03 | Redis 7 for hot index; MinIO (S3 API) for object store in dev; any S3-compatible store in prod | Blueprint §6.1 storage tiers; both self-hostable | Different store needs client swap | `scripts/dev-services.sh` boots both via docker compose | No |
| A-04 | Event log encoding = JSONL, one canonical-JSON record per line, blake3 hash chain | §6.1 "pragmatic default"; greppable; CBOR upgrade path recorded in ADR-004 | CBOR migration later requires re-encode tool | ADR-004; conformance test `chain_fsck` | No |
| A-05 | LLM serving = vLLM with `--enable-prefix-caching`; model = operator-supplied local weights (e.g. an 7–8B instruct model) | INV-4 requires prefix caching; vLLM/SGLang named in §9 | TTFT target missed with larger models | EP-005 M2 latency harness measures TTFT | No |
| A-06 | STT = faster-whisper (streaming, small/int8), VAD = Silero, TTS = Piper (dev default; XTTS/Cartesia swappable), lip-sync = Wav2Lip-class real-time model, mux = GStreamer | §9 self-hostable column, lowest-latency local options | A stage misses its first-chunk budget | Per-stage latency histograms (SPEC-007); every row swappable per §9 | No |
| A-07 | WebRTC: browser client ↔ pod via aiortc in the pod agent; orchestrator relays signaling over WebSocket; coturn for TURN | §3.A signaling responsibilities; aiortc co-located with Python pipeline avoids a hop | aiortc perf ceiling at high fps | Load test in EP-007; LiveKit swap is ADR-007 fallback | No |
| A-08 | GPU cloud = RunPod 4090-class pods with persistent network volume; provider driver abstracted behind a Rust trait | §3.C examples; operator history | Provider API drift | `PodProvider` trait + `mock` driver keeps CI green without cloud creds | No |
| A-09 | Auth = operator-issued opaque bearer tokens (argon2id-hashed at rest) in v1; OIDC is a documented v2 swap | §6.5 requires ownership-gated resume, not a specific IdP | Enterprise SSO needed sooner | SPEC-005; ADR-006 | No |
| A-10 | Per-GPU concurrency cap default = 2 until derived | §8: "5 per 4090" is a hypothesis; renderer-bound systems often land 1–2 | Over-admission collapses latency for all sessions on pod | EP-007 M3 capacity load test derives real cap; config `POD_MAX_SESSIONS` | No |
| A-11 | Client = static HTML/JS (no npm, no bundler) served by the orchestrator | Operator tooling preference: no Node in build chains | Complex UI later wants a framework | Works for v1 single-page client; revisit in ADR if UI grows | No |
| A-12 | Encryption at rest = SSE on the object store (MinIO KMS / S3 SSE-S3) + full-disk encryption on control-plane host; not app-layer envelope crypto in v1 | §6.5 requires encryption at rest; SSE is the smallest correct step | Threat model requiring app-layer crypto | SECURITY.md threat model; ADR-008 documents upgrade path | No |
| A-13 | Session TTL default 90 days; hard delete is immediate and removes events, artifacts, audio refs, Redis index | §6.5 retention + right-to-be-forgotten | Jurisdictional retention rules differ | Operator config `SESSION_TTL_DAYS`; SPEC-002 data rules | No |
| A-14 | Compaction: keep last N=20 turns verbatim; summarize older into frozen rolling summary via a cheap LLM pass at turn boundaries | §6.4 example values | N too small loses useful verbatim context | Config `COMPACT_VERBATIM_TAIL`; measured token sizes in SPEC-007 | No |
| A-15 | Timeline unknown; build sequence follows §10 vertical-slice-first order | Inputs gave no dates | None (ordering, not dates) | ROADMAP.md phases map 1:1 to §10 | No |

Rules: when an agent discovers an assumption is wrong, it records the correction
in the active ExecPlan Decision Log, updates this table, and continues only if
the correction does not trip a STOP condition in AGENTS.md.

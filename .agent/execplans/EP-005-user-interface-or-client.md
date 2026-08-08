# EP-005 — Pipeline + Client (pod agent, streamed stages, barge-in, web client)

## 1. Purpose / Big Picture
Build the thing users feel: the pod agent's streamed, pipelined,
barge-in-capable inference loop (mock GPU stages in CI; real stages behind
`VIHS_REAL_STAGES=1`) plus the static web client. This is the §10 "vertical
slice" made real: after this plan, a scripted E2E conversation with
interruption and resume runs green on a laptop with zero GPUs.

## 2. Scope
`pod/vihs_pod/`: agent bootstrap (register, health, assignment WS, aiortc
peer), turn FSM + AbortBus (SPEC-001 D1/D3), clause chunker (D2), context
assembly (D5), stage Protocols + mock implementations + real-stage adapters
(faster-whisper, vLLM client OR AXIOM gateway client per ADR-012, Piper,
lip-sync stub interface, GStreamer mux), memory_client (append/fetch),
captions data channel, latency instrumentation hooks. `client/`: SPEC-004
flows F1–F7. E2E harness. The LLM stage Protocol gains an `axiom-gateway`
provider (ADR-012): SSE streaming against `VIHS_LLM_URL`/`VIHS_LLM_TOKEN`
with `{messages, model, policy, stream: true, egress: true}` — same
Protocol as the vLLM client, swap-selected by `PROVIDER`.

## 3. Non-goals
Real lip-sync model integration beyond the adapter + a frame-synthesizing
stub (real model lands with hardware in EP-009 staging; the ADAPTER contract
is in scope). No autoscale logic (pods are dumb). No auth beyond dev tokens.
No TURN client logic beyond config passthrough (aiortc handles it).

## 4. Context and Orientation
SPEC-001 is normative and contains the reference FSM/chunker/AbortBus code —
match behavior exactly, then extend. SPEC-004 owns client behavior. The
pipeline is an asyncio task graph; the AbortBus generation counter is the
one concurrency primitive that keeps it honest.

## 5. Files to Read First
SPEC-001 (all), SPEC-004, SPEC-003 (pod-facing rows + signaling), SPEC-006
(pipeline failure states), ARCHITECTURE §6 (budget), TESTING.md (mock rules).

## 6. Files to Change
pod/vihs_pod/{agent.py,turn.py,context.py,memory_client.py,captions.py,
health.py,metrics.py}, pod/vihs_pod/pipeline/{__init__.py,protocols.py,
vad.py,stt.py,llm.py,clause.py,tts.py,lipsync.py,mux.py,ledger.py},
pod/vihs_pod/mocks/{stt.py,llm.py,tts.py,lipsync.py,vad.py},
pod/tests/{test_fsm.py,test_clause.py,test_abort_inv1.py,
test_prefix_stability.py,test_pipeline_flow.py,test_memory_client.py},
client/{index.html,app.js,webrtc.js,session.js,captions.js,styles.css},
tests/e2e/{run_e2e.py,fixtures/*}, requirements.lock (aiortc, av,
faster-whisper*, httpx, websockets, numpy — * real-stage extras group).

## 7. Interfaces and Contracts
```python
# pipeline/protocols.py — every §9 menu row swaps behind these
class STT(Protocol):
    async def stream(self, pcm: AsyncIterator[bytes]) -> AsyncIterator[Partial]: ...
class LLM(Protocol):
    async def stream(self, prompt_segments: PromptSegments) -> AsyncIterator[str]: ...
class TTS(Protocol):
    async def stream(self, clause: str, voice: str) -> AsyncIterator[AudioChunk]: ...
    # AudioChunk carries (pcm, dur_ms, chars_covered) — ledger needs chars/ms
class LipSync(Protocol):
    async def frames(self, audio: AsyncIterator[AudioChunk]) -> AsyncIterator[Frame]: ...
class Mux(Protocol):
    async def push(self, frame: Frame|AudioChunk, clause_id: int, span: tuple[int,int]) -> None
    async def flush_and_report(self) -> list[PlayedSpan]      # INV-1 ledger
```

### Hard piece — the response task graph (pipeline/__init__.py)
```python
async def run_response(gen: int, bus: AbortBus, st: Stages, ctx: PromptSegments,
                       turn_id: int, m: Metrics) -> Committed:
    """Clause-pipelined response. Every queue is bounded (backpressure, not
    unbounded RAM). Every task is bus-guarded. Every handoff checks gen."""
    clauses: asyncio.Queue[Clause] = asyncio.Queue(maxsize=4)
    audio:   asyncio.Queue[Tagged[AudioChunk]] = asyncio.Queue(maxsize=16)

    async def brain():                       # LLM -> clause chunker
        ck, n = ClauseChunker(), 0
        with m.stage("llm_ttft").first_chunk():
            async for delta in st.llm.stream(ctx):
                if gen != bus.gen: return                    # stale: drop
                for c in ck.feed(delta):
                    await clauses.put(Clause(id=(n := n + 1), text=c))
        for c in ck.flush(): await clauses.put(Clause(id=(n := n+1), text=c))
        await clauses.put(END)

    async def voice():                       # clauses -> TTS -> tagged audio
        while (cl := await clauses.get()) is not END:
            start = 0
            with m.stage("tts_ttfa").first_chunk():
                async for ch in st.tts.stream(cl.text, ctx.voice):
                    if gen != bus.gen: return
                    span = (start, start := start + ch.chars_covered)
                    await audio.put(Tagged(ch, cl.id, span))
        await audio.put(END)

    async def face_and_wire():               # audio -> lipsync -> mux (A/V)
        async for tg in drain(audio):
            if gen != bus.gen: return
            with m.stage("lipsync_ttff").first_chunk():
                async for fr in st.lipsync.frames(one(tg.chunk)):
                    await st.mux.push(fr, tg.clause_id, tg.span)
            await st.mux.push(tg.chunk, tg.clause_id, tg.span)

    tasks = [asyncio.create_task(f()) for f in (brain, voice, face_and_wire)]
    for t in tasks: bus.guard(t)
    await asyncio.gather(*tasks)             # CancelledError propagates on abort
    played = await st.mux.flush_and_report() # normal end: full spans
    return commit_from_ledger(turn_id, played, interrupted=False)
```
On ABORT the FSM calls `bus.abort(mux, renderer)`; its returned `played`
ledger builds the committed event: text = concatenation of fully played
spans + proportional cut of the partial chunk (chars_covered × played_ms /
dur_ms, floor to char) — `test_abort_inv1.py` pins this math against fixture
timings byte-exactly.

### Hard piece — context assembly (context.py, INV-4)
```python
@dataclass(frozen=True)
class PromptSegments:
    s0_system: bytes; s1_persona: bytes; s2_memory: bytes   # FROZEN at attach/epoch
    live: list[TurnLine]                                    # S3, fixed template
    voice: str
    def render(self) -> bytes:
        return self.s0_system + self.s1_persona + self.s2_memory + render_live(self.live)
# Rules: s0..s2 are the exact bytes fetched at assignment (memory.md via
# signed URL) — no re-serialization, no timestamps injected, no dict order.
# Epoch change arrives ONLY via a new assignment/refresh from memoryd; the
# pod never edits s2. test_prefix_stability asserts render()[:len(preamble)]
# identical across 10 turns.
```

### Real-stage adapters (config-gated; not CI)
stt.py→faster-whisper streaming w/ partial terminals; llm.py→vLLM
OpenAI-compat stream (`stream=True`), prompt as raw completed segments to
preserve bytes; tts.py→Piper process w/ chunked synth reporting
chars_covered; lipsync.py→adapter contract + stub emitting timed frames;
mux.py→GStreamer appsrc pair → aiortc tracks; vad.py→Silero via onnxruntime.
Each adapter cancel-closes its underlying stream (no orphaned GPU work,
SPEC-001 D3 note).

## 8. Milestones
M1 FSM + chunker + AbortBus units (pure). Validation: `pytest pod -k
   "fsm or clause or abort" -q` — includes INV-1 ledger math + property
   tests. Expected: pass.
M2 Context assembly + memory_client vs live memoryd. Validation: `pytest pod
   -k "prefix or memory_client"` with dev services + memoryd. Expected: pass;
   byte-stability green.
M3 Mock stage pipeline end-to-end in-process. Validation: `pytest pod -k
   pipeline_flow` — scripted turn: endpoint→first mock audio ≤ tuned budget;
   barge-in mid-answer commits fixture prefix exactly.
M4 Agent bootstrap: register/health/assign WS/aiortc loopback + captions.
   Validation: `sh scripts/test-e2e.sh` partial target `e2e_connect`.
M5 Client flows F1–F5 against the stack (mock stages). Validation:
   `sh scripts/test-e2e.sh` E2E OK (full scripted convo + barge-in + resume).
M6 Latency harness + metrics hooks. Validation: harness report shows all
   stage histograms populated; pipeline-overhead assertion (mock stages tuned
   to budget midpoints ⇒ e2e ≤ sum + 150 ms slack) green in CI.
M7 Real-stage adapters compile-checked + unit-mocked (no GPU in CI).
   Validation: `mypy pod/vihs_pod` clean; adapter unit tests with faked
   subprocess/HTTP pass.

## 9. Concrete Steps
Milestone order; asyncio only (CONTRIBUTING); every queue bounded; every
task bus-guarded; cancellation points between chunks only.

## 10. Validation and Acceptance
E2E OK; acceptance = SPEC-001 + SPEC-004 acceptance criteria with mocks; the
kill-pod resume drill is EP-007's, but the resume FLOW (graceful) is green
here.

## 11. Idempotence and Recovery
E2E harness creates+deletes its sessions. Interrupted work: rerun the last
milestone's pytest selector to locate state; mocks are deterministic so
reruns are exact.

## 12. Progress
- [x] M1 units  - [x] M2 context+client  - [x] M3 pipeline  - [x] M4 agent
- [x] M5 client E2E  - [x] M6 latency  - [x] M7 real adapters

## 13. Surprises & Discoveries
### M1-M3
- `run_response`/`abort_response` live in `pipeline/flow.py`, not
  `pipeline/__init__.py` (plan §7); `__init__` re-exports them so the
  package surface matches the plan contract.
- AbortBus added as `pipeline/abort_bus.py` — the SPEC-001 D3 primitive;
  not in the §6 files-to-change list because it is part of the FSM/ledger
  contract (SPEC-001 reference code), not a menu-row adapter.
- Mocks consolidated into `mocks/stages.py` (plan listed per-stage files
  stt/llm/tts/lipsync/vad): one deterministic module keeps CI exact; real
  adapters get their own files in M7.
- `Mux.push` takes `item: object` (plan: `Frame|AudioChunk`) — widens the
  contract without weakening it; call sites enforce the union.
- pyproject.toml needs `asyncio_mode = "auto"` for pytest-asyncio async
  tests; requirements.lock gains pytest-asyncio + httpx (memory client).
- Mock TTS must cover the raw clause text INCLUDING whitespace so ledger
  `chars_covered` math is byte-exact (test_pipeline_flow relies on it).
### M4
- aiortc 1.15 is NON-TRICKLE: candidates are embedded in the local
  description SDP after gathering completes; there are no `icecandidate`
  events and no `RTCIceCandidate.to_dict()` (use `dataclasses.asdict`).
  `RTCDataChannel.send` is synchronous (no await). A single-PC self-answer
  is rejected (`InvalidStateError`) — the loopback needs two PCs exchanging
  full SDP (which is exactly the relay frame flow anyway).
- REAL production bug found by e2e_connect: `registry.ready()` had NO
  production caller — every registered pod stayed Booting and connect
  returned 503 no_capacity. The M1 test called `registry.ready()` directly,
  masking the missing wiring. Fix: the pod's `{"t":"ready"}` on the assign
  WS now performs the readiness transition (SPEC-003 semantics).
- websockets 17 keeps `process_request` for HTTP answers; WS handler is
  single-arg `(ServerConnection)`; `request.path` INCLUDES the query string
  — split on "?" before matching.
- aiortc 1.15 does NOT depend on aiohttp (deps: aioice, av, pyee,
  websockets) — the pod surface uses websockets only.
- numpy 2.5.1 stubs require mypy `python_version = "3.12"` (config was 3.11).
- The long-running dev orchestrator binary was STALE (404 on
  /internal/pods/register); restart services from the current build.
### M5
- `registry.ready()` was wired in M4, but the FULL gate exposed a second
  staleness class: a Ready pod whose assign WS had disconnected (15s reap
  window) still satisfied `fill < cap` and won `pick()` — then the send
  silently dropped the frame while the API returned 200. The e2e harness
  only caught it when all three targets ran in sequence.
- `resume` was derived from `meta.turns` (set 0 at create, never updated) —
  resume ALWAYS false. memoryd's `last_turn_id` is the durable cursor and
  `assign()` already loads it.
- The e2e "convo" target-name mismatch (`"convo"` vs `"e2e_convo"`) made
  `run_e2e.py` fall through to resume — which accidentally proved the
  resume path worked before the resume target existed.
- Mocks that ledger before playback completes double-report cancelled
  chunks as played; the mux ledger also accumulated across turns. Both
  masked barge-in correctness (INV-1) until the harness checked exact
  committed text.
### M6
- The plan §7 `run_response` signature listed `m: Metrics` as a required
  param, but M3 tests call it positionally without one — the metrics param
  is optional (superset contract), preserving all existing callers.
- ruff B023 (lambda binds loop variable) fired on the percentile helper —
  replaced with a closure using default-arg binding.
- `_e2e_ff_recorded` must be a dataclass field, not a dynamic attribute:
  mypy strict rejects `setattr`-style assignments on typed classes.
### M7
- The AXIOM gateway contract was read from the live repo
  (`packages/llm-gateway/src/routes.ts`), not assumed: SSE
  `data: {"content": chunk}`, bearer auth, `policy`/`egress` body fields —
  the adapter matches the actual server, so stage/prod swap needs no
  gateway changes.
- Lazy-imported external deps (`faster_whisper`, `gi`) trip mypy's
  import-not-found under strict mode — each needs an explicit
  `# type: ignore[import-not-found]`, and the lazy loader needs a return
  annotation (`Any`) or `no-untyped-call` fires at every call site.
- `GStreamerMux._ensure` initially hard-imported `gi`, which broke the
  ledger unit test in CI — the ledger is transport-independent and must
  not require GStreamer to exist.

## 14. Decision Log
- D1 (M1-M3): Keep `flow.py` as the task-graph module; re-export from
  `pipeline/__init__.py`. Smallest reversible option; matches §7 surface.
- D2 (M1-M3): `Mux.push(item: object, ...)` instead of the union type —
  widens safely; typed unions enforced at call sites.
- D3 (M1-M3): Consolidated mocks in `mocks/stages.py`; per-stage files
  deferred to M7 with the real adapters they mock.
- D4 (M4): pod local surface via websockets 17 `process_request` (no aiohttp
  — aiortc 1.15 has no aiohttp dependency).
- D5 (M4): new files beyond §6 list — `pod/vihs_pod/webrtc_loopback.py`
  (in-process two-PC media proof) and `tests/e2e/run_e2e.py` (e2e harness,
  the M4/M5 gate). Both are M4/M5 scope; logged per AGENTS §9.
- D6 (M4): env vars `VIHS_POD_ADDR` + `VIHS_POD_TOKEN` added to
  ENVIRONMENT.md registry, .env.example, .env.
- D7 (M4): `test_smoke.py` rewritten — the EP-001 "pod ready" stub contract
  is obsolete; it now pins the URL-builder + SPEC-003 health shape.
- D8 (M4): M4's captions proof is an in-process two-PC loopback; the signal
  WS handler routes to the assignment's SignalBridge and is exercised by the
  browser client in M5.
- D9 (M5): `assign()` derives `resume` from memoryd's durable `last_turn_id`
  cursor, never from the orchestrator's local session cache (create-time
  `turns:0` was never updated → resume always false).
- D10 (M5): router `pick()` is liveness-aware — a Ready pod with a vanished
  assign channel is excluded (stale registry entry); `assign()` re-checks
  liveness before committing and returns no-capacity instead of a silent
  200 with a frame that went nowhere.
- D11 (M5): pod `MemoryClient` converted to async httpx — the sync client
  was awaited inside the asyncio loop (runtime failure); event body matches
  memoryd `v`/`session_id`/`ts` contract.
- D12 (M5): mocks simulate on-wire playback duration; only COMPLETED
  playback is ledgered; mux ledger resets per turn (barge-in must never
  commit the previous turn's spans).
- D13 (M5): conversation loop is task-based (response runs concurrently);
  the awaited-loop version queued barge-in input until the full turn
  finished — INV-1 could never fire.
- D14 (M6): `Metrics` lives in `pod/vihs_pod/metrics.py` (stdlib only);
  `run_response` takes an OPTIONAL `metrics` param so existing callers and
  tests are untouched (the §7 contract signature is a superset).
- D15 (M6): mock stages gain `ttft`/`ttfa`/`ff` delay params (default 0 —
  existing tests stay fast); the M6 latency harness tunes them to the
  ARCHITECTURE §6 budget midpoints (LLM 275 ms, TTS 200 ms, lip-sync
  250 ms) and asserts e2e first-frame ≤ sum + 150 ms slack.
- D16 (M6): `e2e_first_frame` is recorded once per turn (guarded by a
  dataclass field, not a dynamic attribute) at the first audio mux push —
  the pipeline's first-frame completion point.
- D17 (M7): real adapters live in `pipeline/{llm,stt,tts,vad,lipsync,mux}.py`
  (the §6 file list), config-gated — external deps (httpx transport,
  faster-whisper, Piper binary, GStreamer) are imported lazily so CI
  never pays for them. The ADR-012 `axiom-gateway` LLM provider streams
  `POST {VIHS_LLM_URL}/chat/stream` SSE against the AXIOM contract read
  from routes.ts (`data: {"content": chunk}`, bearer token,
  `{messages, model, policy, stream, egress}`).
- D18 (M7): `GStreamerMux` degrades to ledger-only mode when `gi` is
  absent (CI) — the INV-1 ledger is maintained by the pod regardless of
  transport; the real appsrc graph is built at EP-009 staging.
- D19 (M7): `build_stages(real=True)` swaps real adapters behind
  `PROVIDER` (axiom-gateway|vllm|mock); `SileroVAD` uses a deterministic
  RMS energy gate until EP-009 staging (weights path is an S1 credential
  stop), documented in the module.
## 15. Outcomes & Retrospective

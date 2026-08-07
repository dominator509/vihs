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
(faster-whisper, vLLM client, Piper, lip-sync stub interface, GStreamer mux),
memory_client (append/fetch), captions data channel, latency instrumentation
hooks. `client/`: SPEC-004 flows F1–F7. E2E harness.

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
- [ ] M1 units  - [ ] M2 context+client  - [ ] M3 pipeline  - [ ] M4 agent
- [ ] M5 client E2E  - [ ] M6 latency  - [ ] M7 real adapters

## 13. Surprises & Discoveries
## 14. Decision Log
## 15. Outcomes & Retrospective

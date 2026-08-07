# SPEC-001 — Core Domain (turn-taking, streaming, memory semantics)

Status: accepted · Owner: djw · Roadmap phases: 1, 4 · Linked ExecPlans: EP-002, EP-005

## User-visible goal
Conversation FEELS natural: the avatar starts answering fast, stops instantly
when talked over, never freezes visually, and remembers accurately — including
remembering only what it actually got to say.

## Non-goals
Emotion modeling; multi-language auto-switching; speaker diarization
(single-speaker input assumed); wake words.

## Terms
Completable clause; endpoint; abort; committed turn; generated-vs-rendered
text; epoch (see SPEC-000 terms).

## Required behavior

### D1 — Endpointing state machine (pod `turn.py`)
States: `IDLE → USER_SPEAKING → ENDPOINT_PENDING → RESPONDING → IDLE`, plus
`RESPONDING → USER_SPEAKING` on barge-in from anywhere in playback.
- VAD speech-start in IDLE ⇒ USER_SPEAKING; stream audio to STT.
- VAD silence in USER_SPEAKING ⇒ ENDPOINT_PENDING with timer T_sil
  (default 550 ms) AND a semantic check: if the streaming-STT partial ends in
  a strong terminal (sentence-final punct from the ASR, or a filled-pause
  classifier says "done"), shorten to T_fast (default 250 ms). Speech resumes
  ⇒ back to USER_SPEAKING, timers cancelled (this is what "premature
  endpoint" metrics count when we get it wrong).
- Timer fires ⇒ endpoint: freeze the final transcript, enter RESPONDING,
  start the response pipeline.
- In RESPONDING, VAD speech ≥ B_min (default 200 ms of voiced audio, filters
  coughs) ⇒ BARGE-IN: raise ABORT (D3), transition USER_SPEAKING, begin
  transcribing the interruption immediately (its audio is already buffered).

Reference FSM shape the implementation must match (pure logic, injectable
clock, unit-testable without audio):

```python
# pod/vihs_pod/turn.py — transition core (I/O-free; drivers feed it events)
class TurnFSM:
    def __init__(self, clock, t_sil=0.550, t_fast=0.250, b_min=0.200):
        self.state, self.clock = State.IDLE, clock
        self.t_sil, self.t_fast, self.b_min = t_sil, t_fast, b_min
        self.deadline = None; self.voiced_during_playback = 0.0

    def on_vad(self, speech: bool, frame_dur: float, partial_terminal: bool) -> list[Action]:
        s = self.state
        if s is State.IDLE and speech:
            self.state = State.USER_SPEAKING; return [Action.OPEN_STT]
        if s is State.USER_SPEAKING and not speech:
            self.state = State.ENDPOINT_PENDING
            self.deadline = self.clock() + (self.t_fast if partial_terminal else self.t_sil)
            return []
        if s is State.ENDPOINT_PENDING:
            if speech:                       # user kept going — not an endpoint
                self.state = State.USER_SPEAKING; self.deadline = None
                return [Action.COUNT_NEAR_MISS]   # feeds premature-endpoint metric
            if self.clock() >= self.deadline:
                self.state = State.RESPONDING; self.deadline = None
                return [Action.ENDPOINT]     # freeze transcript, start pipeline
            return []
        if s is State.RESPONDING and speech:
            self.voiced_during_playback += frame_dur
            if self.voiced_during_playback >= self.b_min:
                self.state = State.USER_SPEAKING; self.voiced_during_playback = 0.0
                return [Action.ABORT, Action.OPEN_STT]   # barge-in
            return []
        if s is State.RESPONDING and not speech:
            self.voiced_during_playback = 0.0            # cough filter resets
        return []
```

### D2 — Clause-boundary streaming (LLM → TTS handoff)
The LLM token stream is chunked into completable clauses; each clause is
dispatched to TTS the moment it completes — never wait for the full response.
Chunker contract (pure function over an incremental string, `context.py`):
- Emit on: sentence-final `.?!` followed by space/EOS (but NOT inside
  numbers, known abbreviations list, or ellipses); on `,;:` only if the
  pending clause ≥ MIN_CLAUSE_CHARS (default 40) — commas gate long clauses,
  they don't shred short ones; force-emit at MAX_CLAUSE_CHARS (default 240).
- Invariants (property-tested): never emits an empty clause; concatenation of
  emitted clauses + residue == input text exactly; a flush() at stream end
  emits any non-empty residue.

```python
# pod/vihs_pod/pipeline/clause.py — incremental chunker core
ABBREV = {"mr.", "mrs.", "dr.", "e.g.", "i.e.", "etc.", "vs."}
class ClauseChunker:
    def __init__(self, min_chars=40, max_chars=240):
        self.buf = ""; self.min, self.max = min_chars, max_chars
    def feed(self, delta: str) -> list[str]:
        self.buf += delta; out = []
        while True:
            cut = self._boundary()
            if cut is None: break
            out.append(self.buf[:cut]); self.buf = self.buf[cut:].lstrip()
        return out
    def _boundary(self):
        if len(self.buf) >= self.max: return self.max
        for i, ch in enumerate(self.buf):
            nxt = self.buf[i+1:i+2]
            if ch in ".?!" and (nxt == "" or nxt == " "):
                tail = self.buf[max(0, i-4):i+1].lower()
                if any(tail.endswith(a) for a in ABBREV): continue
                if ch == "." and self.buf[i-1:i].isdigit() and nxt.isdigit(): continue
                if nxt == "": return None       # might be mid-token; wait
                return i + 1
            if ch in ",;:" and nxt == " " and i + 1 >= self.min:
                return i + 1
        return None
    def flush(self) -> list[str]:
        out = [self.buf] if self.buf.strip() else []; self.buf = ""; return out
```

### D3 — Barge-in ABORT semantics (asyncio, `turn.py` + pipeline)
One `abort_generation` counter guards the whole response; every stage task is
cancelled; queues flushed; renderer sent to neutral pose; outbound buffer
cleared. Internal completion budget: ≤100 ms. The hard part is INV-1
bookkeeping — knowing exactly what was HEARD:

```python
# Playback ledger: mux reports each audio chunk it actually pushed on-wire,
# tagged with the clause id and char span it covers. On ABORT, the committed
# assistant text = concatenation of fully-played spans + a proportional cut of
# the partially played chunk (chunk carries chars-per-ms from TTS timing).
class AbortBus:
    def __init__(self): self.gen = 0; self._tasks: set[asyncio.Task] = set()
    def guard(self, task): self._tasks.add(task); task.add_done_callback(self._tasks.discard)
    async def abort(self, mux, renderer):
        self.gen += 1                       # stale stages see gen mismatch and drop output
        for t in list(self._tasks): t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        played = await mux.flush_and_report()      # -> list[(clause_id, char_start, char_end)]
        await renderer.neutral_pose()
        return played                              # feeds INV-1 committed text

async def respond(bus: AbortBus, gen: int, stages, transcript: str):
    # every enqueue/dequeue checks `gen == bus.gen` — a cancelled generation's
    # stragglers (an in-flight TTS result, a late frame) are dropped, not played.
    ...
```
Stage tasks must be cancellation-safe: cancellation points only between
chunks; external calls (vLLM stream, TTS socket) wrapped so cancel closes the
underlying stream (no orphaned generation burning GPU).

### D4 — Committed-turn semantics
A turn commits when: (normal) playback of the final clause completes, or
(barge-in) the abort ledger resolves. The committed event's `text` is the
rendered text (INV-1); `meta.interrupted` set accordingly; `meta.latency_ms`
= endpoint→first-audio measured. Uncommitted in-flight work is never logged.

### D5 — Context assembly (INV-4, `context.py`)
Prompt = `S0 system core || S1 persona || S2 memory.md || S3 live turns`.
S0–S2 are BYTE-frozen at session attach / compaction checkpoint: the pod
stores the exact bytes and reuses them every turn of the epoch (no
re-serialization, no timestamp interpolation, no dict-ordering roulette).
S3 appends live turns in fixed template form. Unit test: preamble bytes
identical across 10 consecutive turns; changes only at an epoch boundary.

### D6 — Micro-motion
Renderer receives an idle-driver signal whenever no speech frames are queued
(including USER_SPEAKING): blinks, micro head motion. The puppet never
freezes. (Behavior-level: E2E asserts frames keep flowing while listening.)

## Inputs / outputs
In: 48 kHz mono Opus frames; memory.md bytes; persona/system assets.
Out: committed events (SPEC-002 schema) to memoryd; A/V track; caption deltas.

## Error states
STT stream error mid-turn → retry once, else respond with recovery utterance
event (`kind: note`) and re-listen. LLM stream error → abort response,
apologize-utterance path (fixed string, logged normally). All per SPEC-006
taxonomy.

## Data rules / security rules
Events per SPEC-002; the pod signs appends with its per-assignment token
(SPEC-005); transcript text never logged (OBSERVABILITY).

## Performance rules
Stage first-chunk budgets per ARCHITECTURE §6 table; chunker and FSM are
O(chunk) with no allocation storms (they sit on the hot path).

## Required tests
- FSM: table-driven transitions incl. cough filter, near-miss, barge-in.
- Chunker: property tests (invariants above) + abbreviation/number fixtures.
- AbortBus: cancellation completes ≤100 ms with mock stages; stale-generation
  output dropped; INV-1 ledger math exact on fixture timings.
- Context: byte-stability test (D5).

## Acceptance criteria
E2E (mock stages): scripted long answer + injected barge-in yields a
committed event whose text equals the fixture's played prefix exactly, with
`interrupted: true`; next turn's preamble bytes unchanged; abort-flush metric
≤100 ms in the harness.

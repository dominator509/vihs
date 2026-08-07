# TESTING.md — VIHS

## Test pyramid
1. Unit (fast, no I/O): vihs-core chain/canonicalizer/render; pod pure logic
   (clause chunker, endpointing FSM transitions, context assembly bytes).
2. Integration (local Redis+MinIO via `scripts/dev-services.sh`): memoryd
   append/load/compaction/rebuild; orchestrator↔memoryd HTTP; pod
   memory_client against live memoryd.
3. Contract: every SPEC-003 route has a request/response schema test on both
   producer (Rust) and consumer (Python/JS fixtures) sides.
4. E2E (single-pod loopback, `--mock-gpu`): scripted audio in → scripted
   response out → barge-in → disconnect → resume; asserts INV-1..5 externally.
5. Smoke: `scripts/smoke-test.sh` boots the whole stack and runs one turn +
   one resume.

## Rules by level
- Unit: no network, no clock sleeps (inject clocks), no GPU. Property tests
  (proptest / hypothesis) required for: canonicalizer (arbitrary JSON ⇒ stable
  bytes, sorted keys, float rejection), fsck (mutating any byte of any event
  breaks verification), clause chunker (never emits empty clause; concat of
  clauses == input text).
- Integration: each test owns namespaced keys/prefixes (`test-{uuid}` bucket
  prefix) and deletes them in teardown; tests are parallel-safe.
- E2E: mock GPU stages are the CI default; real-stage runs are opt-in behind
  `VIHS_REAL_STAGES=1` and never gate CI.
- Contract: adding a route without a contract test fails `verify.sh` (route
  registry check in scripts).
- Smoke: must finish < 120 s; used post-deploy too.
- Regression: invariant tests (INV-1 barge-in transcript, INV-2 chain-fsck
  chaos, INV-4 prefix bytes, INV-5 render determinism) are permanent; may be
  extended, never weakened/deleted (AGENTS.md §10).
- Performance: latency harness (EP-005 M6) records per-stage first-chunk
  histograms with mock stages tuned to budget midpoints so pipeline overhead
  regressions are caught in CI; real-hardware numbers come from EP-007.
- Security tests: redaction unit tests (log lines with transcript text fail),
  authz tests (foreign owner → 403 on every session-scoped route), signed-URL
  expiry test.

## Test data / mocking / fixtures
- Golden event logs in `crates/vihs-core/tests/golden/*.jsonl` with expected
  `transcript.md` / `memory.md` bytes — render determinism is byte-exact.
- Deterministic mock stages: mock STT returns scripted transcripts keyed by
  input fixture id; mock LLM streams a fixed token script (including a long
  answer for barge-in tests); mock TTS emits sine-wave opus of deterministic
  duration; mock renderer emits frame counters. All seeded, no wall-clock
  dependence.
- Mocking rule: mock at the stage Protocol boundary only; never mock memoryd
  in integration tests (that's what dev services are for).

## Required tests per feature
Every ExecPlan milestone that adds behavior names its tests; minimum per
feature: 1 happy path, 1 boundary/error path, and the invariant test if the
feature touches an invariant. Bug fixes add a reproducing test first.

## Validation matrix (what runs where)
| Command | unit | integ | contract | e2e | smoke |
|---|---|---|---|---|---|
| test-unit.sh | ✅ | | | | |
| test-integration.sh | | ✅ | ✅ | | |
| test-e2e.sh | | | | ✅ | |
| smoke-test.sh | | | | | ✅ |
| verify.sh | ✅ | ✅ | ✅ | ✅ | ✅ |

## Flaky policy (also EP-007)
A test failing intermittently is quarantined by name in `flaky.txt` (still
runs, non-gating) for max 5 working days with an open note in the active
ExecPlan; then it is fixed or its feature is reverted. Time-dependent
flakiness is fixed by injecting clocks, never by widening sleeps.

## Definition of test done
Named in ExecPlan; passes locally via the scripts; parallel-safe; deterministic
(3 consecutive runs); asserts behavior, not implementation details.

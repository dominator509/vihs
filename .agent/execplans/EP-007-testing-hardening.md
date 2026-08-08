# EP-007 — Testing Hardening (chaos, capacity, coverage)

## 1. Purpose / Big Picture
Prove the invariants under violence and derive the real numbers: chaos suite
(pod kill, memoryd blips, torn-write injection), regression net over critical
flows, capacity load test that DERIVES sessions_per_gpu, flaky policy wiring.

## 2. Scope
Chaos harness (`tests/chaos/`), fault injection points (cfg(test)/env-gated),
loadtest-capacity.sh + latency load harness, coverage gaps closed per
TESTING.md required-tests audit, flaky.txt mechanism in scripts, CI wiring.

## 3. Non-goals
New features. Real-cloud chaos (staging drills are EP-010). Perf tuning
beyond measuring (tuning tickets go to Decision Log/backlog).

## 4. Context and Orientation
(see below)

## 5. Files to Read First
SPEC-006 failure states (each row needs its test), SPEC-008 P-conditions,
ARCHITECTURE §13 capacity formula, TESTING.md flaky policy.

## 6. Files to Change
tests/chaos/{kill_pod_midturn.py,memoryd_pause.py,torn_write_fsck.py,
redis_loss_rebuild.py}, scripts/loadtest-capacity.sh, scripts/test-e2e.sh
(chaos target), pod fault hooks (env-gated `VIHS_FAULT=`), memoryd failpoint
extension, flaky.txt + script support, CI workflow chaos job.

## 7. Interfaces and Contracts
Chaos harness drives ONLY public surfaces (client API, signaling, process
kill) — no test backdoors into state. loadtest-capacity.sh contract: with
real stages on a GPU host, ramps concurrent scripted sessions on one pod,
records per-stage p95 + GPU/VRAM, stops at first budget breach, prints
`sessions_per_gpu = N (binding constraint: <stage|vram>)` and the evidence
table; with mock stages it validates the harness only (CI mode).

## 8. Milestones
M1 kill_pod_midturn: SIGKILL the pod during playback; assert user resumes on
   a fresh pod, committed transcript honors INV-1, chain-fsck fleet-sweep OK.
   Validation: chaos target green.
M2 memoryd_pause: SIGSTOP memoryd 30 s during turns; assert pod buffer
   depth rises then drains, no lost committed turns, media never stalls.
M3 torn_write_fsck + redis_loss_rebuild: corrupt a COPY of a log ⇒ fsck
   catches + integrity_hold on load; FLUSHALL Redis ⇒ rebuild-index restores.
M4 Capacity + latency load harness. Validation: CI-mode run green; REAL run
   documented as the EP-010/staging step (STOP S1 without GPU host — record
   and continue).
M5 Coverage audit vs TESTING.md required-tests + flaky mechanism + CI chaos
   job. Validation: `sh scripts/verify.sh` VERIFY OK with chaos included.

## 9. Concrete Steps
Milestone order; every chaos test asserts through metrics/API evidence, and
tears its sessions down.

## 10. Validation and Acceptance
VERIFY OK incl. chaos; acceptance: SPEC-006 subsystem failure rows each map
to a named green test; capacity harness produces the derivation report
format.

## 11. Idempotence and Recovery
Chaos tests spawn their own processes; safe to re-run; a wedged run is
cleaned by dev-services down/up (dev only).

## 12. Progress
- [x] M1 pod kill  - [x] M2 memoryd pause  - [x] M3 torn/rebuild
- [ ] M4 capacity harness  - [ ] M5 audit+CI

M1 notes (validation `sh scripts/chaos.sh` = kill_pod_midturn OK / CHAOS OK;
full verify.sh includes the chaos gate):
- NEW `tests/chaos/kill_pod_midturn.py` — SPEC-006 "Pod death" row proven on
  public surfaces only: commit turn 1 → send the LONG answer → SIGKILL the
  pod mid-clause-2 playback → poll /admin/pods until the orchestrator marks
  it dead (stale ping >15 s) → start a FRESH pod → resume the SAME session
  (resume=True, cursor last_turn_id≥1) → turn 3 commits on the new pod →
  INV-1 asserted (killed turn's unplayed tail "before finishing." NEVER in
  the transcript) → chain-fsck fleet-sweep via `memoryd --rebuild-index`
  (fscks every session) with no `fsck failed`.
- NEW `scripts/chaos.sh` gate (SKIPs pre-EP-007) + test-e2e.sh `chaos`
  target; verify.sh now runs the chaos gate after test-e2e.
- Findings: (1) the harness keeps ONE WS per conversation — reconnecting the
  same ClientPeer after close is a real aiortc error ("RTCPeerConnection is
  closed"); each connection needs a fresh peer. (2) ClientPeer constructs
  RTCPeerConnection in __init__, which requires a LIVE event loop — the
  chaos script must run entirely under one asyncio.run. (3) `_start_pod_agent`
  without MOCK_ANSWERS silently uses the pod's default scripted answers —
  pass the e2e MOCK_ANSWERS so turn 2 is the long barge-in target.
- Pre-existing (NOT M1-introduced): the dev MinIO store had ~685 orphaned
  session dirs from earlier milestone runs, including 3 with broken chains
  (torn prev_hash, turn_id regressions — dated Aug 7, before EP-006/007
  work). The chaos fsck sweep failed on THOSE until `dev-services.sh
  down/up` flushed the disposable dev store (AGENTS §13). Logged; a clean
  dev store is the documented recovery.

M2 notes (validation `sh scripts/chaos.sh` = memoryd_pause OK; full
verify.sh includes both chaos gates):
- IMPLEMENTED pod AppendBuffer first — a REAL spec gap: the pod appended
  synchronously, violating ARCHITECTURE §9 / SPEC-006 R2 ("the media path
  never blocks on memoryd"). NEW `pod/vihs_pod/append_buffer.py`: bounded 64,
  flusher task with jittered exponential backoff (250 ms base, 5 s cap),
  in-flight tracking (depth never drops to 0 while an append is stuck
  retrying), sticky degrade until drained, media path never awaits memoryd.
- WIRED into `Conversation`: `_append_event` enqueues and returns; flusher
  starts at assignment, best-effort flush at revoke; /health exposes
  `append_buffer_depth` + `append_buffer_degraded` (+ queued/in_flight/
  flusher_alive diagnostics). 7 unit tests in `pod/tests/test_append_buffer.py`.
- NEW `tests/chaos/memoryd_pause.py` — SIGSTOP memoryd 30 s during turns:
  assert depth rises while frozen (samples [3,5,7,7,7,7,7]), turns still
  commit during the freeze (media never stalls), depth drains to 0 after
  SIGCONT, ALL committed turns present (none lost).
- Findings: (1) SIGSTOP via `pgrep -f target/debug/memoryd` returns the bash
  wrapper AND the real child — pids[0] is the wrapper, freezing it is a
  no-op; select the ACTUAL binary process. (2) back-to-back pause turns
  misfired under barge-in semantics (turn counts shift); send one turn at a
  time and wait for each commit. (3) **append durability is ~1 s per event**
  because memoryd's per-request authz runs argon2id verify (M=19 MiB, T=2 —
  OWASP params) on every call; measured 938–1142 ms even for a 404.
  kill_pod_midturn exposed this: under M1's synchronous append, turn 2's
  user event was durable BEFORE playback (argon2 paid pre-playback), so the
  kill always found cursor=2 → first resume commit=3. With the async buffer,
  the flusher's append is still inside the ~1 s argon2 window when the kill
  lands (~0.8 s) → the in-flight user event is lost → cursor=1 → first
  resume commit=2. SPEC-006 explicitly sanctions this ("at most the
  in-flight turn is lost", INV-3); ARCHITECTURE §9 mandates the async path.
  The chaos test was therefore corrected to derive the expected first
  commit from the ACTUAL resume cursor (last_turn_id+1) instead of hardcoding
  3 — it passes for BOTH legitimate outcomes (verified cursor=2 on the
  re-run). Logging: flusher pickups at info (one line per event, aids
  debugging), retries at DEBUG per SPEC-006 logging rules.

## 13. Surprises & Discoveries
- M3: The two chaos drills exposed FIVE real product gaps (GAP-M3-1..5,
  logged with evidence in `.agent/execplans/EP-007-M3-gap-log.md` BEFORE
  fixing, per protocol). All five were spec-mandated behavior the chaos
  suite must demonstrate: 409 integrity_hold on the public surface,
  readyz failing on Redis down, owner restore across rebuild, session
  index warm-up on restart, and retryable (503) upstream token-store
  failures instead of a poisoned 401.
- M3: memoryd's long-lived Redis connection does NOT survive a Redis
  container bounce (`docker stop/start`) — the pooled pipe goes stale and
  EVERY token verify fails until the service is restarted. SPEC-006's
  documented recovery ("on recovery or replacement") is therefore a
  service restart; the redis_loss drill now performs it explicitly and
  asserts the honest sequence (readyz 503 while down → 200 after recovery
  → restart → FLUSHALL → rebuild-index → restart orchestrator → session
  + transcript identical). The connection retry/reconnect config remains
  a backlog item; the 503 retryable classification makes the failure
  honest in the meantime.
- M3: `ensure_services()` (run_e2e) only healthz-checks, and healthz is
  unconditional "ok" — so a stale stack from an earlier drill is
  considered healthy. Chaos drills must establish their OWN known-good
  baseline (restart the services they will fault) instead of trusting
  healthz.
- M2: The pod's "committed turn" logging fires AFTER enqueue, not after the
  event is durable — so "committed" in pod logs means "produced + queued",
  not "in the store". The chaos suite must read durability from memoryd
  (cursor / transcript), not pod log lines alone.
- M2: memoryd append latency is dominated by argon2id verify (~1 s on this
  host, M=19 MiB/T=2, OWASP-specified in SPEC-005). This is the hidden clock
  behind every async-durability window in the chaos tests. A token-verify
  cache (per-token_id, short TTL) would shrink it — flagged for the
  performance backlog, NOT changed here (auth posture is deliberate; SPEC-005
  security rules own the params).
- M2: SIGSTOP'ing the WRONG process (bash wrapper vs the real memoryd
  binary) silently makes the whole chaos scenario a no-op — pgrep -f matches
  both; always select the actual binary PID.

## 14. Decision Log
- M2: Implement AppendBuffer BEFORE writing the chaos test (the buffer is a
  spec requirement ARCHITECTURE §9 / SPEC-006 R2, and the pause test's whole
  premise is that the pod buffers). No mock path: the buffer is the
  production behavior being proven.
- M2: Kill-pod resume commit expectation is cursor-derived (last_turn_id+1),
  NOT hardcoded turn 3. Rationale: async buffering (mandated by §9) means
  the in-flight turn's user event may legitimately be lost on kill (INV-3),
  so the first commit after resume is 2 OR 3 depending on whether the
  flusher beat the kill. The test asserts the INVARIANT (first durable turn
  after the resume cursor commits; transcript honors INV-1) which holds in
  both cases.
- M2: Flusher retries log at DEBUG (SPEC-006: "retries log at debug, not
  info — no log storms"); pickup at INFO (once per event, aids debugging).
  Health surfaces depth/degraded for operators instead of log noise.
- M2: Exposed extra buffer diagnostics in /health (queued, in_flight,
  flusher_alive) beyond depth/degraded — additive only; no consumer breakage.
- M3: Chaos tests probe PUBLIC surfaces only (orchestrator 8080 + memoryd
  8091 HTTP). Rationale: a drill that exercises internal APIs can pass while
  the user-visible contract is broken; the M3 drills were deliberately
  surface-level and that is exactly how the five gaps surfaced.
- M3: Torn-write fixture = mutate a payload field keeping JSON valid
  (invalid JSON would fail parsing, not chain verification). fsck
  recomputes hashes, so a valid-JSON payload mutation produces BadHash →
  integrity_hold — the honest "torn" signal.
- M3: `--rebuild-index` does NOT auto-repair a torn chain: it fscks, logs
  `fsck failed`, and exits 0, leaving the session under integrity_hold.
  This is deliberate (SPEC-006: chain verification failure = integrity_hold,
  never silent repair); the drill asserts the session stays 409 after
  rebuild.
- M3: redis_loss drill restarts memoryd + orchestrator at START (known-good
  baseline) and AGAIN after Redis recovery — the second restart is the
  SPEC-006-documented "on recovery or replacement" recovery, and the first
  is the drill's own precondition. The orchestrator restart also re-seeds
  the .env admin/pod tokens into the wiped store (tokens live in Redis).
- M3: teardown must be state-aware. First verify.sh run after the drill
  FAILED at the mcpd contract gate (`upstream: memoryd http error sending
  request`) because the drill's `finally` killed the memoryd/orchestrator
  it had restarted — even though both were ALREADY running when the gate
  began (verify.sh runs chaos between test-e2e and build/smoke). Fix:
  capture `pre_running` from `ss -tlnp` before the drill and, in teardown,
  stop only restarted services that were NOT pre-existing. Verified both
  ways: standalone (starts + cleans up) and in-gate (restarts + leaves up).

## 15. Outcomes & Retrospective
M2 delivered the R2 append buffer (real spec gap found while scoping: pod
appended synchronously, media path could block on memoryd) + the
memoryd_pause chaos proof: buffer depth rises under freeze (samples
[3,5,7,7,7,7,7]), 3 turns commit while memoryd is frozen (media never
stalls), drains to 0 after SIGCONT, zero turns lost. Full verify.sh GREEN
with BOTH chaos gates (kill_pod_midturn + memoryd_pause) in the permanent
gate. Remaining risk documented: ~1 s per-append argon2 latency bounds how
much of the in-flight turn survives a pod kill (INV-3-sanctioned); a
token-verify cache is the backlog item if per-append durability matters in
production.

M3 delivered the two chaos proofs + FIVE product gap fixes:
- torn_write_fsck: corrupt a COPY of a log (valid JSON, payload mutated →
  BadHash) → public resume returns 409 integrity_hold, memoryd /load 409,
  rebuild-index fscks and logs `fsck failed`, session stays 409 (never
  auto-repaired). GREEN.
- redis_loss_rebuild: create + commit → Redis DOWN (readyz 503 on BOTH
  services) → recover (readyz 200) → restart memoryd (fresh connection,
  GAP-M3-5) → FLUSHALL → old token 401 → rebuild-index restores index +
  owner → restart orchestrator (tokens re-seeded, SessionIndex warmed
  from owner zsets, GAP-M3-4) → fresh token → session visible + transcript
  IDENTICAL to the pre-loss snapshot. GREEN.
- Gap fixes (all logged first, verified live): GAP-M3-1 orchestrator
  surfaces memoryd 409 integrity_hold as 409 (was 503); GAP-M3-2 readyz
  pings Redis on both services (503 while down, verified live); GAP-M3-3
  rebuild/heal restore owner + owner zset from the create-note meta.owner;
  GAP-M3-4 SessionIndex warm-up on orchestrator restart; GAP-M3-5 memoryd
  authz classifies upstream token-store failures as 503 retryable (never
  a poisoned 401). memoryd suites: 21 tests green (authz 10, integ 10,
  lib 1).
- Remaining risks: (1) memoryd's Redis connection has no retry/reconnect
  config — a Redis bounce requires the documented service restart; the 503
  retryable classification keeps that honest. (2) ~1 s argon2 per-append
  latency (M2 backlog, unchanged). (3) cargo-audit RUSTSECs via
  aws-smithy (pre-existing, `|| echo` swallow).
Next: M4 capacity/latency harness (CI-mode; real run is the EP-010/staging
step, STOP S1 without a GPU host — RUNPOD_API_KEY is the only missing
credential, needed at EP-009).

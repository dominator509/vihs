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
- [x] M1 pod kill  - [ ] M2 memoryd pause  - [ ] M3 torn/rebuild
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

## 13. Surprises & Discoveries
## 14. Decision Log
## 15. Outcomes & Retrospective

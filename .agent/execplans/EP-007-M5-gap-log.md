# EP-007 M5 Gap Log — coverage audit vs TESTING.md / SPEC-006

Status: ALL FOUR GAPS FIXED + VERIFIED. Logged with evidence BEFORE
fixing, per protocol. M5 validation: `sh scripts/verify.sh` VERIFY OK
with chaos included; SPEC-006 acceptance: "each subsystem failure row
maps to a named green test" — now 6/6.

## Coverage matrix (SPEC-006 "Failure states by subsystem")

| Row | Required behavior | Test | Status |
|-----|-------------------|------|--------|
| 1 | Pipeline stage crash mid-turn: abort cleanly (AbortBus), emit `kind:note` `stage_error` (no user text), speak fixed recovery utterance, return to listening; 2 crashes/session → pod degraded → orchestrator drains | — | **GAP-M5-1: no impl, no test** |
| 2 | Pod death: stale ping >15 s → dead; resume path; INV-3/INV-1 | tests/chaos/kill_pod_midturn.py | GREEN |
| 3 | memoryd down: pods buffer (R2); resume 503 retryable | tests/chaos/memoryd_pause.py | GREEN |
| 4 | Redis down: readyz 503; rebuild-index recovery | tests/chaos/redis_loss_rebuild.py | GREEN |
| 5 | Chain verification failure: 409 integrity_hold, never auto-repair | tests/chaos/torn_write_fsck.py | GREEN |
| 6 | Signaling abuse (schema/size): `bad_signal`, close WS after 3 strikes | — | **GAP-M5-2: mechanism is dead code, no test** |

## GAP-M5-1 — pipeline stage crash has no failure-mode implementation

**Evidence:** `pod/vihs_pod/pipeline/flow.py` `run_response` uses
`asyncio.gather(*tasks)` with NO exception handling — a stage exception
propagates out of `_handle_turn` (conversation.py:225), and `_response_loop`
spawns `_handle_turn` as a detached task, so the exception becomes an
unhandled task error. SPEC-006 row 1 requires: clean abort (AbortBus), a
`kind:note` event `stage_error` with NO user text, a fixed recovery
utterance spoken, return to listening, and after TWO stage crashes the pod
marks itself degraded so the orchestrator can drain it. None of that exists
(grep for `stage_error` in pod/vihs_pod and tests: zero hits).

## GAP-M5-2 — signaling-abuse 3-strike mechanism is dead code

**Evidence:** `crates/orchestrator/src/signal.rs` defines
`client_read_loop` with `MAX_STRIKES=3` and `bad_signal`-style strike
counting, but grep shows NO call site — the live signal route
(`signal_route.rs` handle_signal c2p pump) does inline
`validate_client_frame(&v).is_ok()` and silently DROPS invalid frames
(`continue`). SPEC-006 row 6 requires: `bad_signal` error frame + close WS
after 3 strikes. The 16 KiB frame cap (`MAX_FRAME_BYTES`) is also only in
the dead function, not the live path.

## GAP-M5-3 — flaky.txt mechanism does not exist

**Evidence:** TESTING.md:64-68 defines the flaky policy (quarantine by name
in `flaky.txt`, still runs non-gating, max 5 working days, then fix or
revert). No `flaky.txt` file, no script support reads/quarantines by name.

## GAP-M5-4 — CI chaos job not explicit

**Evidence:** `.github/workflows/ci.yml` runs `sh scripts/verify.sh` which
since M3/M4 includes the `chaos` and `loadtest-capacity` gates — so chaos
DOES run in CI. The ExecPlan scope (§6 "CI workflow chaos job") is
satisfied via verify.sh; make it explicit (job/step name + artifact) so a
chaos failure is distinguishable in the workflow UI.

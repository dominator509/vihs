# EP-007 M4 Gap Log — capacity/latency harness findings

Status: BOTH GAPS FIXED + VERIFIED. Logged per protocol (gaps with
evidence BEFORE fixing; fixes verified live).

## GAP-M4-1 — assignment slot never released on client disconnect (BLOCKING)

**Observed:** First live CI-mode run of `tests/load/capacity.py`:
```
stage 1: llm_ttft=1ms tts_ttfa=0ms lipsync_ff=17ms ...   (OK)
stage 2: ... connect session failed: 503 {'code': 'no_capacity', ...}
```
The harness creates n concurrent sessions per ramp stage on ONE pod
(cap=3), closes the stage's peers, and waits for pod fill to drain
(`wait_pod_fill(0)`). Stage 2's connect got `no_capacity` because the
stage-1 assignment slot was never freed.

**Root cause (code trace):**
- Pod side (`pod/vihs_pod/agent.py` `handle_revoke`): assignments are
  only removed from `_assignments` when the pod receives a `{"t":"revoke"}`
  frame on its assign WS. `fill = len(self._assignments)`.
- Orchestrator side (`crates/orchestrator/src/signal_route.rs`
  `handle_signal`): when the client WS closed, the handler just ended the
  pumps and called `st.relay.unbind(&connection_id)` — it NEVER pushed a
  `revoke` frame to the pod's assign channel (`st.pod_assign.senders`).
- Result: pod kept the conversation in `_assignments` forever → fill
  stayed 1 → `router::pick` saw no Ready pod with `fill < cap` → 503
  `no_capacity` for the next session. A REAL production leak: a dead
  client pins a pod slot until the pod dies or is restarted.

**Fix (all three signal-route exit paths now revoke):**
1. `RelayHandle.routes` value changed from `String` (pod id) to
   `RelayRoute { pod_id, session_id }` — the revoke frame needs session_id.
2. `connect_session` binds with `session_id`.
3. `handle_signal` exit paths:
   - client close / pod side close (main pump end): push revoke + unbind.
   - pod gone: push revoke (best-effort) + unbind.
   - pod-ward WS connect failure: push revoke + unbind (client can never
     reach the pod; session must be reconnectable).
4. Pumps restructured with `tokio::select!` (client→pod vs pod→client) so
   a client-only close with a SILENT pod still tears the relay down — the
   old `while let Some(...) = pod_stream.next()` blocked forever waiting
   for a pod frame that would never come.
5. NEW `crate::signal::revoke_frame(session_id, connection_id)` helper +
   2 unit tests (frame shape, relay route round-trip). 71 orchestrator
   tests green.

**Verification (live):**
- `capacity.py` CI mode: stages 1-3 all connect, turn, drain to fill=0
  between stages → `LOADTEST OK`, sessions_per_gpu = 3.
- e2e_resume log proves the lifecycle:
  `assigned ... resume=False → assignment revoked → assigned ... resume=True`
- Orchestrator restart required to pick up the new binary (verified the
  running PID was older than the rebuilt binary before restarting).

## GAP-M4-2 — pod signal WS routed all connections to one conversation (LATENT)

**Observed while auditing for stage ≥2 correctness:** the capacity ramp
connects N concurrent ClientPeers, each opening its own pod-ward signal WS
(`/internal/pods/{id}/signal?connection_id=…`). The pod's `signal_handler`
routed EVERY connection to `self._conversation` — the single
last-assigned conversation pointer. With 2+ concurrent sessions, the first
peer's offer would be fed into the last conversation's bridge: the first
peer's datachannels never open, `peer.connect()` times out. Not reached in
the first failed run (stage 2 died at no_capacity), but would block any
ramp ≥2.

**Root cause:** `signal_handler` used `self._conversation.bridge`
(one pointer) instead of resolving the conversation by the
`?connection_id=` query param on the WS path.

**Fix:** route by `connection_id` → conversation in `_assignments`
(values carry `connection_id`); unknown connection → close 1008.
2 new pod tests (routing isolation across two simultaneous connections;
unknown connection rejected). 76 pod tests green.

**Verification (live):** capacity.py stage 2 and 3 (2-3 concurrent peers)
both complete their turns and read per-stage p95 from the pod.

## Findings (harness env, not product gaps)

- `_start_pod_agent` pod_env was `{**os.environ, **load_env(.env)}` —
  .env WINS, so the harness's `POD_MAX_SESSIONS=3` never reached the pod
  (registered cap stayed 2) → stage-3 third connect failed no_capacity.
  Flipped to `{**load_env(.env), **os.environ}`: process env (test/harness
  overrides) wins over .env defaults. The pod's own explicit keys
  (VIHS_POD_ADDR/TOKEN/LOG) still win over both.
- Harness read pod /health immediately after the caption — but e2e_total
  records at the END of run_response (after lipsync first frame + mux
  flush). Reading early raced injected latencies: SIM_BREACH's 600 ms
  lipsync sleep never showed (lipsync_ff/e2e_total = no samples), so the
  breach was never detected. Added `wait_metrics_settled(n)` — polls
  until `metrics.e2e_total.count >= n` (the LAST stage) before reading.
  SIM_BREACH now detects the 621 ms lipsync_ff > 400 ms budget at stage 1.

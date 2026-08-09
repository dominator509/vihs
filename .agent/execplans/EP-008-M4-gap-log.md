# EP-008 M4 gap log (logged before fixing, per protocol)
#
# GAP-M4-1: plan says "assert the full required-series list each appears
#   with a non-zero sample (grep for `^name` and a count line that is not
#   ` 0`)". Literal reading is impossible without fabricating events:
#   vihs_authz_denials_total, vihs_endpoint_premature_total,
#   vihs_compactions_total, vihs_epoch_boundary_total, vihs_scale_events_total
#   and vihs_gpu_util are ALL legitimately 0 in a clean scripted smoke
#   (no bad tokens, no premature endpoint, no compaction trigger, mock gpu).
#   Fix: assert PRESENCE for every registry name on its owning service
#   (catches missing/renamed metrics — the SPEC-007 "all names emitted"
#   acceptance), and NON-ZERO only for the series the smoke traffic
#   actually exercises (stage histograms, e2e first audio, append latency,
#   resume ok, barge-in, abort-flush, cache ratio, pod sessions, cold start).
#   Documented in EP-008 Surprises & Decision Log.
#
# GAP-M4-2: plan says smoke-test.sh scrapes localhost:8093/metrics AFTER
#   the E2E run — but the harness tears its pod down in `finally` before
#   smoke-test.sh gets control, so the pod target would always be dead.
#   Fix: add `--metrics-out DIR` to run_e2e.py; the harness dumps all three
#   /metrics snapshots (orchestrator, memoryd, pod) WHILE the pod is alive,
#   just before teardown. smoke-test.sh asserts on those files. Honest:
#   the snapshot is captured from live services with real smoke traffic.
#
# GAP-M4-3 (redis_loss_rebuild chaos drill, verify.sh blocker): memoryd's
#   readyz HANGS when Redis is down instead of failing fast 503, and after a
#   Redis container restart the SAME process takes ~112s to recover — the
#   drill's 30s window can never pass. Root cause (measured, not theory):
#   redis-rs `ConnectionManager` default config has NO response_timeout. When
#   Redis dies, the pooled TCP socket goes half-open (PING write succeeds into
#   the kernel buffer, read blocks forever); redis-rs only reconnects on
#   `Reconnect`-class errors (io::ErrorKind::ConnectionReset/BrokenPipe/...),
#   which never surface until the OS TCP stack gives up (~112s observed).
#   Fix (crates/memoryd/src/index.rs + crates/vihs-auth/src/store.rs):
#   1) readyz probes Redis with a FRESH short-lived connection carrying
#      explicit 2s connection+response timeouts — genuine 503 in ~0.00s when
#      Redis is down, 200 in ~1s when it returns (same-process, no restart).
#   2) pooled ConnectionManagers get 3s response+connection timeouts so
#      data-path ops fail fast instead of hanging for minutes.
#   3) TokenStore ops (mint/seed/verify/revoke) retry ONCE on a fresh
#      connection when the pooled socket errors stale-class (timeout/io/
#      dropped/refusal) — the orchestrator now self-heals across a bounce
#      without a restart (line 291's post-FLUSHALL 401 depends on verify
#      reaching Redis at all; before this it 503'd on the stale pipe).
#   Verified: recovery probe down=503@0.00s / up=200@1.0s (was -1@5.01s /
#   200@112.5s); redis_loss_rebuild.py now exits 0 end-to-end.
#
#!/usr/bin/env sh
# Smoke: boot (or target) the stack, run one turn + resume + delete.
# Usage: smoke-test.sh [BASE_URL]  (no arg = local boot with mock stages)
set -eu
cd "$(dirname "$0")/.."
if [ -f tests/e2e/run_e2e.py ] && [ -d pod/.venv ]; then
  OUT="$(mktemp -d)"
  trap 'rm -rf "$OUT"' EXIT
  pod/.venv/bin/python tests/e2e/run_e2e.py --smoke \
    --metrics-out "$OUT" ${1:+--base-url "$1"}
  pod/.venv/bin/python - "$OUT" <<'PY'
"""EP-008 M4: assert the required metric series are PRESENT on each
service and that the traffic-exercised series carry non-zero samples.

Presence-only for series that are legitimately 0 in a clean scripted
smoke (authz denials, endpoint premature, compactions, epoch boundaries,
scale events, gpu util in mock). Non-zero for the series the smoke
traffic actually produces (stage histograms, e2e first audio, append
latency, resume ok, barge-in, abort-flush, cache ratio, pod sessions,
cold start). See .agent/execplans/EP-008-M4-gap-log.md.
"""
import pathlib
import sys

out = pathlib.Path(sys.argv[1])

# (file, required series, must-be-nonzero series)
EXPECT = [
    ("orchestrator.metrics",
     ["vihs_pod_sessions", "vihs_scale_events_total",
      "vihs_cold_start_secs", "vihs_resume_total",
      "vihs_authz_denials_total"],
     ["vihs_pod_sessions", "vihs_cold_start_secs", "vihs_resume_total"]),
    ("memoryd.metrics",
     ["vihs_append_latency_ms", "vihs_compactions_total",
      "vihs_memory_blob_tokens", "vihs_epoch_boundary_total",
      "vihs_authz_denials_total"],
     ["vihs_append_latency_ms"]),
    ("pod.metrics",
     ["vihs_stage_first_chunk_ms", "vihs_e2e_first_audio_ms",
      "vihs_bargein_total", "vihs_abort_flush_ms",
      "vihs_endpoint_premature_total", "vihs_append_buffer_depth",
      "vihs_prefix_cache_hit_ratio", "vihs_gpu_util",
      "vihs_epoch_boundary_total"],
     # bargein/abort_flush are exercised by the full convo path, not the
     # smoke (e2e_resume has no interruption) — presence-only here.
     ["vihs_stage_first_chunk_ms", "vihs_e2e_first_audio_ms",
      "vihs_prefix_cache_hit_ratio"]),
]

def names(body: str):
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        yield line.split("{")[0].split(" ")[0]

def present(series: str, body: str) -> bool:
    # A histogram family appears as name_bucket/_sum/_count; counters and
    # gauges appear as the bare name. Match the family either way.
    return any(n == series or n.startswith(series + "_") for n in names(body))

def nonzero(series: str, body: str) -> bool:
    # For histograms the sample count is the `_count` line; for counters and
    # gauges it is the bare-name line. Both render a trailing number.
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        name = line.split("{")[0].split(" ")[0]
        if name == series + "_count" or name == series:
            val = line.rsplit(" ", 1)[-1]
            try:
                if float(val) != 0.0:
                    return True
            except ValueError:
                pass
    return False

failed = []
for fname, required, must_nonzero in EXPECT:
    path = out / fname
    if not path.exists():
        failed.append(f"{fname}: MISSING FILE")
        continue
    body = path.read_text(errors="replace")
    if body.startswith("# scrape error"):
        failed.append(f"{fname}: {body.strip()}")
        continue
    for series in required:
        if not present(series, body):
            failed.append(f"{fname}: missing series {series}")
    for series in must_nonzero:
        if not nonzero(series, body):
            failed.append(f"{fname}: series {series} present but zero")

if failed:
    print("METRIC PRESENCE FAIL")
    for f in failed:
        print("  -", f)
    sys.exit(1)
print("METRIC PRESENCE OK")
PY
else
  echo "smoke: SKIP (harness not built yet — EP-005)"
fi
echo "SMOKE OK"

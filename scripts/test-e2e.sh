#!/usr/bin/env sh
# E2E: single-pod loopback with mock GPU stages (CI-safe). EP-005 wires the
# harness; EP-007 adds the chaos targets (kill_pod_midturn etc.).
set -eu
cd "$(dirname "$0")/.."
if [ -f tests/e2e/run_e2e.py ] && [ -d pod/.venv ]; then
  if [ "${1:-}" = "chaos" ]; then
    shift
    # Chaos targets: tests/chaos/*.py, each a self-contained public-surface
    # fault-injection script that returns 0 on green.
    if [ "$#" -gt 0 ]; then
      for t in "$@"; do
        pod/.venv/bin/python "tests/chaos/${t}.py"
      done
    else
      for t in tests/chaos/*.py; do
        pod/.venv/bin/python "$t"
      done
    fi
    echo "E2E CHAOS OK"
    exit 0
  fi
  pod/.venv/bin/python tests/e2e/run_e2e.py "$@"
else
  echo "e2e: SKIP (harness not built yet — EP-005)"
fi
echo "E2E OK"

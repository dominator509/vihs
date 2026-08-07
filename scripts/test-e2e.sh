#!/usr/bin/env sh
# E2E: single-pod loopback with mock GPU stages (CI-safe). EP-005 wires the
# harness; until then this SKIPs behind the marker.
set -eu
cd "$(dirname "$0")/.."
if [ -f tests/e2e/run_e2e.py ] && [ -d pod/.venv ]; then
  pod/.venv/bin/python tests/e2e/run_e2e.py "$@"
else
  echo "e2e: SKIP (harness not built yet — EP-005)"
fi
echo "E2E OK"

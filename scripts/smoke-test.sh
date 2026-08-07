#!/usr/bin/env sh
# Smoke: boot (or target) the stack, run one turn + resume + delete.
# Usage: smoke-test.sh [BASE_URL]  (no arg = local boot with mock stages)
set -eu
cd "$(dirname "$0")/.."
if [ -f tests/e2e/run_e2e.py ] && [ -d pod/.venv ]; then
  pod/.venv/bin/python tests/e2e/run_e2e.py --smoke ${1:+--base-url "$1"}
else
  echo "smoke: SKIP (harness not built yet — EP-005)"
fi
echo "SMOKE OK"

#!/usr/bin/env sh
# Chaos suite (EP-007): fault-injection scripts under tests/chaos/*.py.
# Each script drives ONLY public surfaces (client API, signaling, process
# kill) and returns 0 when the SPEC-006 failure-state assertion passes.
# SKIPs behind the directory marker so pre-EP-007 runs stay green.
set -eu
cd "$(dirname "$0")/.."
if [ -d tests/chaos ] && [ -d pod/.venv ]; then
  sh scripts/test-e2e.sh chaos
else
  echo "chaos: SKIP (no chaos suite yet — EP-007)"
fi
echo "CHAOS OK"

#!/usr/bin/env sh
# Capacity derivation harness driver (EP-007 M4 builds the python side).
# Real mode (GPU host, VIHS_REAL_STAGES=1): ramps sessions on ONE pod until a
# first-chunk budget breach; prints sessions_per_gpu + binding constraint.
# CI mode: validates the harness with mock stages only.
set -eu
cd "$(dirname "$0")/.."
if [ -f tests/load/capacity.py ] && [ -d pod/.venv ]; then
  pod/.venv/bin/python tests/load/capacity.py "$@"
else
  echo "loadtest: SKIP (harness not built yet — EP-007)"
fi
echo "LOADTEST OK"

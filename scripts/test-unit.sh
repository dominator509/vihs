#!/usr/bin/env sh
# Unit tests only: no network, no services (TESTING.md pyramid level 1).
set -eu
cd "$(dirname "$0")/.."
if [ -f Cargo.toml ]; then cargo test --workspace --lib --bins; else echo "unit: SKIP rust"; fi
if [ -d pod/.venv ] && [ -d pod/tests ]; then
  sh scripts/pytest-gate.sh pod -q -m "not integration and not e2e"
else echo "unit: SKIP python"; fi
echo "UNIT OK"

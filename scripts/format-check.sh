#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
if [ -f Cargo.toml ]; then cargo fmt --all -- --check; else echo "format: SKIP rust"; fi
if [ -d pod/.venv ] && [ -d pod/vihs_pod ]; then
  pod/.venv/bin/ruff format --check pod
else echo "format: SKIP python"; fi
echo "FORMAT OK"

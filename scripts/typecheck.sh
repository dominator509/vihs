#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
if [ -f Cargo.toml ]; then cargo check --workspace --all-targets; else echo "typecheck: SKIP rust"; fi
if [ -d pod/.venv ] && [ -d pod/vihs_pod ]; then
  pod/.venv/bin/mypy pod/vihs_pod
else echo "typecheck: SKIP python"; fi
echo "TYPECHECK OK"

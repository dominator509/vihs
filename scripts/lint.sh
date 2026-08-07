#!/usr/bin/env sh
# Lint: clippy -D warnings + ruff + layer-import grep gates. Read-only.
set -eu
cd "$(dirname "$0")/.."
if [ -f Cargo.toml ]; then
  cargo clippy --workspace --all-targets -- -D warnings
  # Layer gates (ARCHITECTURE §3): vihs-core imports no sibling crates; and
  # no std::fs inside vihs-core/src except bin/ (EP-002 rule).
  if [ -d crates/vihs-core/src ]; then
    if grep -RIn --include='*.rs' -e 'memoryd' -e 'orchestrator' crates/vihs-core/src >/dev/null 2>&1; then
      echo "LINT FAIL: vihs-core references sibling crates" >&2; exit 1
    fi
    if grep -RIn --include='*.rs' 'use std::fs' crates/vihs-core/src \
        | grep -v '/bin/' >/dev/null 2>&1; then
      echo "LINT FAIL: I/O in vihs-core library code" >&2; exit 1
    fi
  fi
else echo "lint: SKIP rust (no workspace yet)"; fi
if [ -d pod/.venv ] && [ -d pod/vihs_pod ]; then
  pod/.venv/bin/ruff check pod
else echo "lint: SKIP python (no pod yet)"; fi
echo "LINT OK"

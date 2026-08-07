#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
if [ -f Cargo.lock ]; then
  command -v cargo-audit >/dev/null 2>&1 \
    && cargo audit \
    || echo "audit: NOTE cargo-audit not installed (see ENVIRONMENT.md tools)"
else echo "audit: SKIP rust (no lockfile)"; fi
if [ -d pod/.venv ] && [ -f pod/requirements.lock ]; then
  pod/.venv/bin/pip install --quiet pip-audit 2>/dev/null || true
  pod/.venv/bin/pip-audit -r pod/requirements.lock \
    || { echo "AUDIT FAIL: python vulnerabilities" >&2; exit 1; }
else echo "audit: SKIP python"; fi
echo "AUDIT OK"

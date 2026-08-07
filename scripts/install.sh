#!/usr/bin/env sh
# Install: rust workspace fetch + pod venv from lockfile. Idempotent.
set -eu
cd "$(dirname "$0")/.."
if [ -f Cargo.toml ]; then cargo fetch; else echo "install: SKIP cargo (no workspace yet)"; fi
if [ -f pod/pyproject.toml ]; then
  PY="python3.11"; command -v "$PY" >/dev/null || PY=python3
  [ -d pod/.venv ] || "$PY" -m venv pod/.venv
  pod/.venv/bin/pip install --quiet --upgrade pip
  if [ -f pod/requirements.lock ]; then
    pod/.venv/bin/pip install --quiet -r pod/requirements.lock
  fi
  pod/.venv/bin/pip install --quiet -e pod
else
  echo "install: SKIP pod (no pyproject yet)"
fi
echo "INSTALL OK"

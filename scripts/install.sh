#!/usr/bin/env sh
# Install: rust workspace fetch + pod venv from lockfile. Idempotent.
set -eu
cd "$(dirname "$0")/.."
if [ -f Cargo.toml ]; then cargo fetch; else echo "install: SKIP cargo (no workspace yet)"; fi
PY="python3.11"; command -v "$PY" >/dev/null || PY=python3
if [ -f pod/pyproject.toml ]; then
  [ -d pod/.venv ] || "$PY" -m venv pod/.venv
  pod/.venv/bin/pip install --quiet --upgrade pip
  if [ -f pod/requirements.lock ]; then
    pod/.venv/bin/pip install --quiet -r pod/requirements.lock
  fi
  pod/.venv/bin/pip install --quiet -e pod
else
  echo "install: SKIP pod (no pyproject yet)"
fi
# Provision .env: the e2e + python harnesses require .env at the repo root
# (tests/e2e/run_e2e.py, pod/tests) and CI checks out no .env (gitignored).
# Never overwrite an existing .env; fill secret placeholders with fresh
# dev-only values.
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  fill_secret() {
    # Replace the key's placeholder line (commented or bare, whatever the
    # placeholder value: empty, ***, <...>). The .env was just copied from
    # .env.example so each key is present exactly once.
    val="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed -i "s|^#* *$1=.*|$1=$val|" .env
  }
  fill_secret VIHS_TOKEN_PEPPER
  fill_secret VIHS_ADMIN_TOKEN
  fill_secret VIHS_POD_TOKEN
  echo "install: generated .env from .env.example (dev-only secrets)"
fi
echo "INSTALL OK"

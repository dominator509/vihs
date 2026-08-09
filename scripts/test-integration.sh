#!/usr/bin/env sh
# Integration + contract tests. Requires dev services (Redis+MinIO) up.
set -eu
cd "$(dirname "$0")/.."
if [ -f deploy/docker/compose.dev.yml ]; then
  docker compose -f deploy/docker/compose.dev.yml ps --status running 2>/dev/null \
    | grep -q . || { echo "INTEGRATION FAIL: dev services not running (scripts/dev-services.sh up)" >&2; exit 1; }
fi
if [ -f Cargo.toml ]; then
  if find crates -maxdepth 2 -type d -name tests | grep -q .; then
    cargo test --workspace --test '*'
  else
    echo "integration: SKIP rust (no integration tests yet — EP-003)"
  fi
else echo "integration: SKIP rust"; fi
if [ -d pod/.venv ] && [ -d pod/tests ]; then
  if grep -RIn -e 'integration' pod/tests >/dev/null 2>&1; then
    sh scripts/pytest-gate.sh pod -q -m integration
  else
    echo "integration: SKIP python (no integration-marked tests yet — EP-003)"
  fi
else echo "integration: SKIP python"; fi
# Post-suite chain sweep (EP-003 acceptance): fsck every log the suite made.
if [ -x target/debug/chain-fsck ] && [ -d .test-artifacts/logs ]; then
  for f in .test-artifacts/logs/*.jsonl; do
    [ -e "$f" ] || break
    target/debug/chain-fsck "$f" >/dev/null || { echo "INTEGRATION FAIL: chain sweep $f" >&2; exit 1; }
  done
fi
echo "INTEGRATION OK"

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
    # The mcpd/orchestrator contract suites call a LIVE memoryd over HTTP
    # (SPEC-003; the tests document "Requires dev services (memoryd, Redis,
    # MinIO)"). Start it here so CI and fresh dev boxes don't depend on a
    # manual `cargo run -p memoryd` from COMMANDS.md.
    : "${VIHS_REDIS_URL:=redis://127.0.0.1:6379}"
    : "${VIHS_S3_ENDPOINT:=http://127.0.0.1:9000}"
    : "${VIHS_S3_BUCKET:=vihs-sessions}"
    : "${VIHS_S3_ACCESS_KEY:=minioadmin}"
    : "${VIHS_S3_SECRET_KEY:=minioadmin}"
    : "${VIHS_MEMORYD_ADDR:=127.0.0.1:8091}"
    if [ -z "${VIHS_TOKEN_PEPPER:-}" ]; then
      if [ -f .env ]; then
        VIHS_TOKEN_PEPPER="$(sed -n 's/^VIHS_TOKEN_PEPPER=//p' .env | head -n 1 | tr -d '\r')"
      fi
      # Test-only pepper (>=16 chars per memoryd validation). Exported below
      # so the cargo-test processes mint tokens with the same pepper the
      # live memoryd verifies (the tests' ensure_shared_pepper prefers env).
      : "${VIHS_TOKEN_PEPPER:=ci-integration-test-pepper-0123456789abcdef}"
    fi
    export VIHS_REDIS_URL VIHS_S3_ENDPOINT VIHS_S3_BUCKET VIHS_S3_ACCESS_KEY \
      VIHS_S3_SECRET_KEY VIHS_MEMORYD_ADDR VIHS_TOKEN_PEPPER
    # Build the service binaries: memoryd runs below for the contract suites;
    # the e2e gate (scripts/test-e2e.sh) starts target/debug/orchestrator itself.
    cargo build -p memoryd -p orchestrator
    mkdir -p .test-artifacts
    ./target/debug/memoryd > .test-artifacts/memoryd-ci.log 2>&1 &
    MEMD_PID=$!
    trap 'kill $MEMD_PID 2>/dev/null || true' EXIT INT TERM
    i=0; until curl -sf "http://${VIHS_MEMORYD_ADDR}/readyz" >/dev/null 2>&1; do
      i=$((i+1))
      if [ "$i" -gt 60 ]; then
        echo "INTEGRATION FAIL: memoryd not ready at $VIHS_MEMORYD_ADDR" >&2
        echo "--- .test-artifacts/memoryd-ci.log ---" >&2
        tail -n 30 .test-artifacts/memoryd-ci.log >&2 || true
        exit 1
      fi
      sleep 1
    done
    cargo test --workspace --test '*'
    kill $MEMD_PID 2>/dev/null || true
    trap - EXIT INT TERM
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

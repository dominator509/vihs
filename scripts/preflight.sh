#!/usr/bin/env sh
# Preflight: toolchain + services + env sanity. Safe: read-only checks.
set -eu
cd "$(dirname "$0")/.."
fail() { echo "PREFLIGHT FAIL: $1" >&2; exit 1; }
skip() { echo "preflight: SKIP $1 (marker absent — pre-EP-001 tree)"; }
command -v git >/dev/null || fail "git missing"
command -v jq  >/dev/null || fail "jq missing"
command -v cargo >/dev/null || fail "cargo missing (Rust 1.79+)"
command -v docker >/dev/null || fail "docker missing"
PY="python3.11"; command -v "$PY" >/dev/null || PY=python3
command -v "$PY" >/dev/null || fail "python3.11 missing"
"$PY" -c 'import sys; assert sys.version_info[:2] >= (3, 11)' 2>/dev/null \
  || fail "python >= 3.11 required"
# Marker-gated: workspace pieces exist only after EP-001 (SKIP, not FAIL).
[ -f Cargo.toml ] && cargo metadata --no-deps >/dev/null 2>&1 \
  || { [ -f Cargo.toml ] && fail "Cargo.toml present but workspace broken"; skip "cargo workspace"; }
[ -d pod/.venv ] || skip "pod venv (run scripts/install.sh)"
if [ -f deploy/docker/compose.dev.yml ]; then
  docker compose -f deploy/docker/compose.dev.yml ps >/dev/null 2>&1 \
    || echo "preflight: NOTE dev services not up (scripts/dev-services.sh up)"
else
  skip "dev services compose file"
fi
[ -f .env ] || echo "preflight: NOTE .env missing (cp .env.example .env)"
echo "PREFLIGHT OK"

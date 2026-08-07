#!/usr/bin/env sh
# Full verification roll-up. Order matters: cheap gates first.
set -eu
cd "$(dirname "$0")/.."
for s in preflight lint format-check typecheck test-unit test-integration \
         test-e2e build security-check dependency-audit smoke-test; do
  echo "== verify: $s =="
  sh "scripts/$s.sh"
done
echo "VERIFY OK"

#!/usr/bin/env sh
# Machine-checkable subset of PRODUCTION_READINESS.md (SPEC-008 P1 + parts).
set -eu
cd "$(dirname "$0")/.."
sh scripts/verify.sh
# Cap must not be the undertided default on a readiness run (SPEC-008 P2).
if [ -f .env ]; then
  cap="$(grep -E '^POD_MAX_SESSIONS=' .env | cut -d= -f2 || true)"
  note="$(grep -E '^# CAP_DERIVED=' .env | cut -d= -f2 || true)"
  if [ -n "$cap" ] && [ "$note" != "yes" ]; then
    echo "PROD-READY FAIL: POD_MAX_SESSIONS not marked derived (# CAP_DERIVED=yes after loadtest)" >&2
    exit 1
  fi
fi
# Non-goal grep gate (PROJECT_BRIEF): forbidden feature surface absent.
if grep -RIn --include='*.rs' --include='*.py' -e 'billing' -e 'group_call' \
     crates pod 2>/dev/null | grep -v -e test -e '#' >/dev/null; then
  echo "PROD-READY FAIL: non-goal surface detected" >&2; exit 1
fi
echo "PROD-READY OK (automated subset; drills per PRODUCTION_READINESS.md still required)"

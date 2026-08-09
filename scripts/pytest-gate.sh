#!/usr/bin/env sh
# pytest gate with flaky quarantine (TESTING.md flaky policy, EP-007 M5).
#
# Runs pytest normally — the test ALWAYS runs. If the run fails, the
# failures are compared against the quarantine list in `flaky.txt` (one
# test nodeid substring per line; '#' comments allowed). Failures whose
# nodeid matches a flaky.txt entry are NON-GATING: they print a WARNING
# (with the open-note reminder) and do not fail the gate. Any failure NOT
# in flaky.txt fails the gate.
#
# Policy: a quarantined test may stay in flaky.txt at most 5 working days
# with an open note in the active ExecPlan; then it is fixed or its
# feature is reverted (TESTING.md).
set -eu
cd "$(dirname "$0")/.."
ROOT="$PWD"
FLAKY_FILE="${FLAKY_FILE:-$ROOT/flaky.txt}"

if [ ! -f "$FLAKY_FILE" ]; then
  echo "pytest-gate: WARN no $FLAKY_FILE — creating empty (flaky policy)"
  : > "$FLAKY_FILE"
fi

# Capture the pytest invocation; use --tb=short and full nodeids.
set +e
pod/.venv/bin/pytest -q --tb=short --no-header "$@" > /tmp/pytest-gate.out 2>&1
RC=$?
set -e

if [ "$RC" -eq 0 ]; then
  cat /tmp/pytest-gate.out
  echo "PYTEST GATE OK"
  exit 0
fi

# Extract failing nodeids: lines like `tests/foo.py::test_bar FAILED` or
# `FAILED tests/foo.py::test_bar - AssertionError`.
FAILED_NODEIDS=$(grep -E '^(FAILED|.*FAILED) ' /tmp/pytest-gate.out \
  | sed -E 's/^(FAILED|.*FAILED) //; s/ - .*$//' | sort -u || true)

# Quarantine check: every failing nodeid must match a flaky.txt entry.
MISSING=""
for nid in $FAILED_NODEIDS; do
  if ! grep -vE '^\s*(#|$)' "$FLAKY_FILE" | grep -Fq "$nid"; then
    MISSING="$MISSING $nid"
  fi
done

cat /tmp/pytest-gate.out
if [ -n "$MISSING" ]; then
  echo "PYTEST GATE FAIL: failures not in flaky.txt:$MISSING" >&2
  exit 1
fi

echo "PYTEST GATE WARN: only flaky-quarantined failures (see flaky.txt — max 5 working days, open ExecPlan note required)" >&2
exit 0

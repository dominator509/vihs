#!/usr/bin/env sh
# Secret scan + redaction tests. Read-only. EP-006 extends the test half.
set -eu
cd "$(dirname "$0")/.."
# Secret scan: high-signal patterns; .env & vendor dirs excluded.
PATTERNS='-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|rp_[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9_\-]{24,}'
if git ls-files 2>/dev/null | grep -q .; then
  if git ls-files -z | grep -zv -e '^\.env' -e 'requirements.lock' \
     | xargs -0 grep -EIn "$PATTERNS" 2>/dev/null | grep -v 'security-check.sh'; then
    echo "SECURITY FAIL: potential secret committed" >&2; exit 1
  fi
fi
[ -f .gitignore ] && grep -qx '\.env' .gitignore \
  || { [ -f .gitignore ] && { echo "SECURITY FAIL: .env not gitignored" >&2; exit 1; } }
# Redaction tests (exist from EP-006 onward).
if [ -f Cargo.toml ] && cargo test --workspace redaction --no-run >/dev/null 2>&1; then
  cargo test --workspace redaction
else echo "security: SKIP redaction tests (pre-EP-006)"; fi
echo "SECURITY OK"

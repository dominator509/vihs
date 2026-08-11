#!/usr/bin/env sh
# EP-010 M4 / SPEC-008 P6 restore drill.
#
# 1. Snapshot the live sessions bucket to a scratch backup bucket.
# 2. Run memoryd --rebuild-index pointed at the SCRATCH bucket (restore):
#    every session chain in the backup is fsck'd from the restored data.
# 3. Assert "rebuild-index: done" with no "fsck failed".
#
# The live bucket is untouched; the scratch bucket is deleted at the end.
set -eu
cd "$(dirname "$0")/.."

ALIAS=local
SRC=vihs-sessions
TS="$(date +%Y%m%d-%H%M%S)"
SCRATCH="vihs-sessions-backup-${TS}"
MC_BIN="docker exec docker-minio-1 mc"

echo "P6: snapshot ${SRC} -> ${SCRATCH}"
$MC_BIN mb "$ALIAS/$SCRATCH" >/dev/null
$MC_BIN cp --recursive "$ALIAS/$SRC/" "$ALIAS/$SCRATCH/" >/dev/null
n_objects="$($MC_BIN ls --recursive "$ALIAS/$SCRATCH" 2>/dev/null | wc -l)"
echo "P6: snapshot complete (${n_objects} objects)"

# Restore + fsck sweep: run memoryd --rebuild-index against the scratch bucket.
# memoryd reads VIHS_S3_BUCKET from env/.env; override to the scratch bucket.
# Load the rest of .env (VIHS_REDIS_URL etc.) without exporting secrets.
set -a
# shellcheck disable=SC1091
. ./.env
set +a
echo "P6: fsck sweep on restored scratch bucket"
VIHS_S3_BUCKET="$SCRATCH" timeout 900 ./target/debug/memoryd --rebuild-index \
    > /tmp/p6-rebuild.log 2>&1
rc=$?
out="$(cat /tmp/p6-rebuild.log)"
echo "$out" | tail -5
if [ "$rc" -ne 0 ] || ! echo "$out" | grep -q "rebuild-index: done"; then
    echo "P6 FAIL: rebuild-index did not complete" >&2
    $MC_BIN rb --force "$ALIAS/$SCRATCH" >/dev/null 2>&1 || true
    exit 1
fi
if echo "$out" | grep -q "fsck failed"; then
    echo "P6 FAIL: chain-fsck found a torn chain in the backup" >&2
    $MC_BIN rb --force "$ALIAS/$SCRATCH" >/dev/null 2>&1 || true
    exit 1
fi

echo "P6: restore drill OK (${n_objects} objects fsck'd clean from scratch)"
$MC_BIN rb --force "$ALIAS/$SCRATCH" >/dev/null
echo "P6 OK"

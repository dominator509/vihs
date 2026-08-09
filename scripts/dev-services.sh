#!/usr/bin/env sh
# Local Redis + MinIO (+ optional observability profile). Dev only.
# Usage: dev-services.sh up|down|up-obs
set -eu
cd "$(dirname "$0")/.."
F=deploy/docker/compose.dev.yml
[ -f "$F" ] || { echo "dev-services: SKIP (compose file not created yet — EP-001)"; echo "DEV-SERVICES OK"; exit 0; }
case "${1:-up}" in
  up)
    docker compose -f "$F" up -d redis minio
    i=0; until docker compose -f "$F" exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; do
      i=$((i+1)); [ "$i" -gt 30 ] && { echo "DEV-SERVICES FAIL: redis" >&2; exit 1; }; sleep 1
    done
    i=0; until curl -sf http://127.0.0.1:9000/minio/health/ready >/dev/null 2>&1; do
      i=$((i+1)); [ "$i" -gt 30 ] && { echo "DEV-SERVICES FAIL: minio" >&2; exit 1; }; sleep 1
    done
    docker compose -f "$F" run --rm mc mb -p "local/${VIHS_S3_BUCKET:-vihs-sessions}" >/dev/null 2>&1 || true
    ;;
  up-obs)
    docker compose -f "$F" --profile obs up -d
    i=0
    until curl -sf http://127.0.0.1:${VIHS_OBS_PROM_PORT:-9090}/api/v1/targets >/dev/null 2>&1; do
      i=$((i+1)); [ "$i" -gt 30 ] && { echo "dev-services: prometheus not ready" >&2; exit 1; }
      sleep 1
    done
    i=0
    until curl -sf http://127.0.0.1:${VIHS_OBS_GRAFANA_PORT:-3100}/api/health >/dev/null 2>&1; do
      i=$((i+1)); [ "$i" -gt 30 ] && { echo "dev-services: grafana not ready" >&2; exit 1; }
      sleep 1
    done
    ;;
  down-obs)
    docker compose -f "$F" --profile obs down
    ;;
  down) docker compose -f "$F" down -v ;;
  *) echo "usage: dev-services.sh up|down|up-obs|down-obs" >&2; exit 2 ;;
esac
echo "DEV-SERVICES OK"

# VIHS control-plane install (staging/production) — EP-009 M4

Installs the control plane (memoryd + orchestrator + Redis + object store) on
a host. Pods are provider-managed (RunPod) and NOT part of this install.

## Prerequisites

- Ubuntu 24.04 host, public reachability for `VIHS_ORCH_ADDR` (pods must
  reach the orchestrator to register — DEPLOYMENT.md).
- Docker (Redis 7 + MinIO containers) — same images as dev
  (`deploy/docker/compose.dev.yml`), plus the release binaries.
- `.env` in place (copy `.env.example`, fill; RUNPOD_API_KEY + PROVIDER=runpod
  for the provider driver).

## Steps

```sh
# 1. Build (on a build host, or use the CI release artifacts)
sh scripts/build.sh            # release binaries + pod wheel

# 2. Lay down the app dir
sudo mkdir -p /opt/vihs
sudo cp -a target/release/{memoryd,orchestrator} /opt/vihs/target/release/
sudo cp .env /opt/vihs/.env
sudo chmod 600 /opt/vihs/.env

# 3. Dev services (Redis + MinIO + bucket) as systemd-managed docker
sh scripts/dev-services.sh up

# 4. Install units — ORDER MATTERS (DEPLOYMENT.md: memoryd before
#    orchestrator so writers recover tips first)
sudo cp deploy/systemd/vihs-memoryd.service /etc/systemd/system/
sudo cp deploy/systemd/vihs-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vihs-memoryd
sudo systemctl enable --now vihs-orchestrator

# 5. Verify
curl -s http://127.0.0.1:8091/readyz   # expect 200
curl -s http://127.0.0.1:8080/readyz   # expect 200
```

## Rollback (see ROLLBACK.md / RELEASE.md)

Redeploy the previous tag's binaries + restart units. Event log is
append-only and tolerant one `v` either way — no data rollback.

## Teardown

```sh
sudo systemctl disable --now vihs-orchestrator vihs-memoryd
sh scripts/dev-services.sh down
```

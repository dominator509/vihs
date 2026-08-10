# DEPLOYMENT.md — VIHS

## Environments
dev (local), staging (1 GPU pod, real stages), production. Same images, env
differs only (ENVIRONMENT.md parity rule).

## Deployment architecture
Control plane: one host (or container pair) running orchestrator + memoryd +
Redis + object store endpoint (self-hosted MinIO or S3/R2), behind a TLS
reverse proxy; coturn beside it. Data plane: provider-managed GPU pods from
`deploy/docker/pod.Dockerfile`, mounting the persistent network volume
(weights + base assets) read-mostly at `VIHS_MODEL_DIR` — no multi-GB
downloads on cold start. Models stay resident once loaded; use provider warm
containers / snapshot-restore where supported; target cold start 20–30 s,
masked by the client idle loop.

## Build artifacts
- Rust release binaries `orchestrator`, `memoryd`, `chain-fsck` (build.sh).
- Pod wheel + `pod.Dockerfile` image tagged `vihs-pod:<git-sha>`.
- Client static files served by orchestrator (embedded at build).

## Release flow (detail in RELEASE.md)
main green → tag vX.Y.Z → CI builds artifacts + image → staging deploy →
staging smoke (scripts/smoke-test.sh against staging URL) → manual approval →
production deploy.

## Deployment steps (per environment)
1. `sh scripts/build.sh` (DOCKER=1 for pod image); push image to registry.
2. Update provider template to new image tag (`deploy/runpod/`); do NOT
   recycle live pods yet.
3. Deploy control plane binaries (systemd units in `deploy/`), restart
   memoryd then orchestrator (this order: writers recover tips first).
4. Roll pods: mark old template drained; autoscaler replaces on natural churn
   or force-drain per pod (sessions survive via resume path — announce brief
   reconnect in stage; in prod prefer natural churn).
5. Run post-deploy smoke; verify metrics dashboards.

## Migration steps
None relational. Event `v` bumps require readers deployed BEFORE writers emit
the new version (control plane first, then pods).

## Rollback steps
See ROLLBACK.md. Binaries/images are versioned; event log is append-only and
forward/backward tolerant one `v` either way, so rollback = redeploy previous
tag; no data rollback exists or is needed. Verified by drill on staging
(EP-009 M5): `deploy/runpod/rollback-drill.py` rolls the pod image back one
tag and asserts the live release via `--expect-release` — 97.8s rollback leg,
SMOKE OK, 0 pods left billing.

## Post-deploy smoke tests
`scripts/smoke-test.sh <base-url>`: create session → one scripted turn (staging
uses a real short utterance fixture) → disconnect → resume → assert recalled
turn → hard-delete the smoke session. Must print SMOKE OK.

## Required approvals / STOP conditions
Production deploy requires explicit operator approval (AGENTS.md STOP S6 —
coding agents never deploy to prod autonomously). Staging is agent-allowed
when EP-009 is active and RUNPOD_API_KEY is provided (else STOP S1).

## Production verification
Post-deploy: smoke green; p50 first-audio within budget on the latency
dashboard for 30 min; error-rate and abort-flush alerts quiet; one manual
resume spot-check.

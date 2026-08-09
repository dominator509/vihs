# EP-009 — Deployment & Release

## 1. Purpose / Big Picture
Ship it to staging for real: pod Docker image with the full inference stack,
RunPod provider driver + templates + network-volume layout, CI/CD release
pipeline, staging deploy + real-stage smoke, release/rollback flows wired.

## 2. Scope
deploy/docker/pod.Dockerfile (CUDA base, GStreamer, venv, model-volume
mount), provider_runpod.rs implementing PodProvider, deploy/runpod/
(template JSON + VOLUME.md layout: models/{stt,llm,tts,lipsync}, assets/),
control-plane systemd units + install doc, .github release workflow
(tag→build→push→staging deploy gate), CHANGELOG.md bootstrap, staging env
docs, real-stage smoke on staging, cold-start measurement.

## 3. Non-goals
Production deploy (EP-010 gate + STOP S6). Multi-provider drivers beyond
RunPod (trait already seats them). Autoscaler policy changes.

## 4. Context and Orientation
(see below)

## 5. Files to Read First
DEPLOYMENT.md (normative flow/order), RELEASE.md, ADR-007/008, ENVIRONMENT
staging vars, provider trait in orchestrator.

## 6. Files to Change
deploy/** as above, crates/orchestrator/src/provider_runpod.rs (+ feature
flag), .github/workflows/release.yml, CHANGELOG.md, ENVIRONMENT.md staging
confirmations, scripts/smoke-test.sh (accept base-url arg — already spec'd).

## 7. Interfaces and Contracts
provider_runpod implements deploy/terminate/cold_start_hint via RunPod API;
integration-tested against a recorded-fixture HTTP mock in CI; live calls
only in staging with RUNPOD_API_KEY (STOP S1 if absent when the staging
milestone starts — report and hold that milestone only).

## 8. Milestones
M1 Pod image builds + runs mock-stage agent in container locally.
   Validation: `DOCKER=1 sh scripts/build.sh` BUILD OK; container healthz.
M2 RunPod driver + fixture tests + template/volume docs. Validation: driver
   tests green against mock HTTP.
M3 Release workflow (tag → artifacts + image). Validation: dry-run workflow
   green on a test tag.
M4 STAGING: deploy control plane + one real pod; real-stage smoke; cold-start
   + first latency numbers recorded. Validation: `sh scripts/smoke-test.sh
   https://staging…` SMOKE OK. (Requires operator creds — S1 gate.)
M5 Release/rollback docs verified by doing: roll staging back one tag, smoke.
   Validation: rollback ≤10 min, SMOKE OK (feeds SPEC-008 P7).

## 9. Concrete Steps
Milestone order; image layers ordered for cache (deps → code); weights NEVER
in the image (volume only).

## 10. Validation and Acceptance
BUILD OK, staging SMOKE OK, rollback drill done; acceptance: DEPLOYMENT.md
steps executed as written (doc bugs fixed in the same plan).

## 11. Idempotence and Recovery
(re-runnable as stated)

## 12. Progress
Deploys are re-runnable; staging sessions cleaned. - [x] M1 - [x] M2 - [ ] M3
- [ ] M4 - [ ] M5

## 13. Surprises & Discoveries
- M1: `nvidia/cuda:12.4.1-base-ubuntu24.04` does not exist on Docker Hub; the
  12.4.1 line tops out at ubuntu22.04. Used `12.9.2-base-ubuntu24.04` (verified
  via Docker Hub tags API) — Ubuntu 24.04 ships Python 3.12, matching the pod's
  `requires-python >=3.11` and mypy `python_version = 3.12`.
- M1: `httpx==0.28.1` is a direct runtime dependency (agent.py, memory_client.py
  import it at module level) but was MISSING from pod/requirements.lock — a
  fresh venv from the lock would crash the pod at import. Added to the lock.
- M1: runtime deps are installed in the image as an explicit pinned set
  (aiortc/av/websockets/numpy/httpx) — dev tools (pytest/mypy/ruff) stay out of
  the container.
- M2: driver fixture tests use a local axum fake on an ephemeral port (tokio
  spawned task on the test runtime — a cross-thread `from_std` listener hits
  tokio's "Registering a blocking socket" panic).
- M2: RunPod `DELETE /v2/pods/{id}` is the TERMINATE (permanent) call; 404 is
  treated as success so teardown is idempotent. RunPod's own "stop" action
  would keep billing — the driver never uses it.
## 14. Decision Log
- M1: base image `nvidia/cuda:12.9.2-base-ubuntu24.04` (12.4.1 lacks a
  ubuntu24.04 tag; 12.9.2 is the current 12.x line with it). Python 3.12 ships
  in the base → venv matches local mypy target.
- M1: container installs the runtime dep subset pinned from requirements.lock;
  dev-only tools (pytest/mypy/ruff) are excluded from the image (smaller, no
  tooling attack surface). httpx added to the lock (real missing-dep fix).
- M1: `.dockerignore` at repo root excludes `.env`, `target/`, `pod/.venv`,
  git, node_modules — secrets and 2.7k-venv files never enter the build context.
- M2: `RUNPOD_API_KEY` absent with PROVIDER=runpod → hard `exit(1)` at startup
  (fail fast, loud) rather than silent mock fallback — honest S1 gate. Unknown
  PROVIDER values still fall back to mock with a warning (dev ergonomics).
- M2: pod name prefix `vihs-` + spec id; container env carries VIHS_POD_ID /
  POD_MAX_SESSIONS / VIHS_REAL_STAGES=1 so the agent self-identifies on the
  real GPU pod. VIHS_RUNPOD_ENV JSON merge allows operator extra env.
## 15. Outcomes & Retrospective

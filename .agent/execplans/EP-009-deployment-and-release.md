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
Deploys are re-runnable; staging sessions cleaned. - [ ] M1 - [ ] M2 - [ ] M3
- [ ] M4 - [ ] M5

## 13. Surprises & Discoveries
## 14. Decision Log
## 15. Outcomes & Retrospective

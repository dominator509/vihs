# Changelog

All notable changes to VIHS are documented here, grouped by release tag
(SemVer `vX.Y.Z` on main). Format follows RELEASE.md: Added / Changed /
Fixed / Security, with SPEC/EP references. Event-schema `v` bumps are called
out explicitly with the reader-first deploy note.

## [Unreleased]

### Added
- Pod Docker image (`deploy/docker/pod.Dockerfile`, CUDA 12.9 base, GStreamer,
  runtime-dep venv) and repo `.dockerignore` — EP-009 M1.
- RunPod provider driver (`crates/orchestrator/src/provider_runpod.rs`) with
  fixture-tested deploy/terminate against the v2 API; `PROVIDER=runpod` S1 gate
  — EP-009 M2.
- Release observability + rollback drill (EP-009 M5): pod registers
  `VIHS_RELEASE` as `versions.pod`; GET /admin/pods exposes versions;
  run_e2e.py `--expect-release` / smoke-test.sh `VIHS_EXPECT_RELEASE` assert
  the live release; `deploy/runpod/rollback-drill.py` rolls staging back one
  tag with a release assertion. Drill result: rollback leg 97.8s, SMOKE OK,
  0 pods left billing (2026-08-10).

### Changed
- `pod/requirements.lock`: added `httpx==0.28.1` (missing direct runtime dep).
- ENVIRONMENT.md / .env.example: `VIHS_RUNPOD_*` rows; `VIHS_RELEASE` row.
- staging-deploy.py: process env wins for `VIHS_RUNPOD_IMAGE`/`VIHS_RELEASE`
  so the rollback drill can pin per-leg image tags.

## [Unreleased] — earlier milestones
See git history for EP-001 through EP-008 commits (`4295cd7` … `478a6f6`).

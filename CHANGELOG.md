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

### Changed
- `pod/requirements.lock`: added `httpx==0.28.1` (missing direct runtime dep).
- ENVIRONMENT.md / .env.example: `VIHS_RUNPOD_*` rows.

## [Unreleased] — earlier milestones
See git history for EP-001 through EP-008 commits (`4295cd7` … `478a6f6`).

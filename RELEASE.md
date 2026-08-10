# RELEASE.md — VIHS

## Release types
- Regular (minor/patch): features + fixes through full flow below.
- Hotfix (patch): critical fix; may skip staging soak but never smoke.

## Versioning & changelog
SemVer tags `vX.Y.Z` on main. CHANGELOG.md entry per release: Added/Changed/
Fixed/Security, with SPEC/EP references. Event-schema `v` bumps are called out
explicitly with reader-first deploy note.

## Branch strategy
Trunk-based: short-lived branches → PR → main. Tags only from green main.

## Release candidate criteria
verify.sh green on the tagged commit; dependency audit reviewed (criticals
block); invariant suite green; CHANGELOG updated.

## Release checklist (also .agent/checklists/release.md)
1 Confirm RC criteria. 2 Tag. 3 CI builds artifacts + pod image `:vX.Y.Z`.
4 Deploy staging (DEPLOYMENT.md order: memoryd → orchestrator → pod roll).
5 Staging smoke green + 30-min dashboard soak. 6 Operator approval recorded.
7 Production deploy. 8 Post-deploy smoke + verification (DEPLOYMENT.md).
9 Monitor alerts for 24 h; hotfix or rollback on trigger (ROLLBACK.md).

## Release tags & rollback (EP-009 M5)
Releases are SemVer tags (`vX.Y.Z`); the pod image is pushed with the same
tag (`ttl.sh/vihs-pod-slimlauncher:vX.Y.Z`). The pod registers its release as
`versions.pod` (env `VIHS_RELEASE`, default derived from the image tag) and
GET /admin/pods exposes it — so staging smoke can assert which release is
live (`smoke-test.sh` with `VIHS_EXPECT_RELEASE`, or run_e2e.py
`--expect-release`). Rollback = redeploy the previous image tag; validated by
the EP-009 M5 drill (97.8s rollback leg, SMOKE OK).

## Release notes
Human-readable summary from CHANGELOG; note any operator action required
(new env vars from ENVIRONMENT.md diff, cap re-derivation on model change).

## Post-release monitoring
First 24 h: latency + resume + abort-flush dashboards; alert pages route to
operator; regression triggers rollback per ROLLBACK.md.

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
Deploys are re-runnable; staging sessions cleaned. - [x] M1 - [x] M2 - [x] M3
- [x] M4 - [x] M5

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
- M3: repo has NO git remote and the `dominator509/vihs` GitHub repo does not
  exist, so the release workflow cannot be triggered on a real tag push.
  Validation = local dry-run: create `v0.1.0-test-dryrun` tag, run the
  workflow's steps (verify.sh → build.sh → docker build) locally, confirm
  green, drop the tag. GHCR push steps stay documented but unexercised until
  the repo exists.
- M3: PyYAML parses the Actions `on:` key as boolean True (YAML 1.1) — the
  trigger structure is intact under GitHub's own parser (push + workflow_dispatch).
- M5: staging-deploy.py merges env as `{**os.environ, **load_env(.env)}` —
  `.env` WINS, so the drill's per-leg `VIHS_RUNPOD_IMAGE`/`VIHS_RELEASE`
  overrides were silently ignored and every leg would deploy the mutable
  `:24h` tag. Fixed: explicit process-env wins for those two keys.
- M5: orchestrator restarted WITHOUT `WARM_POOL_FLOOR` in its env (capture
  pattern missed it) → default floor 1 fired the mock provider's floor
  bootstrap. Restarted with floor=0 explicitly; registry clean.
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
- M3: release workflow triggers on `v*` tag push + manual `workflow_dispatch`
  with a `staging` boolean input (the M4 staging gate). `GITHUB_TOKEN` with
  `packages: write` handles GHCR; release artifacts (binaries + pod wheel) are
  attached via `gh release create/upload`. Staging gate only fires with the
  `RUNPOD_API_KEY` secret configured and the M4 deploy script present — honest
  placeholder until M4 wires the real deploy.
- M3: CHANGELOG.md bootstrapped with the Unreleased section covering M1–M2.
- M4 (honest blockers, logged before spending):
  - **smoke-test.sh `--base-url` is accept+ignore** (`run_e2e.py` line 618:
    "Remote-target form is not used by local verify; accept+ignore"). Pointing
    the smoke at staging would silently test the LOCAL stack — the plan's own
    M4 validation command does not currently exercise staging. Gap: wire the
    remote path (target staging orchestrator, use the pod registered there).
  - **Real-stage deps NOT in the image or lock**: `faster_whisper` (STT),
    `piper-tts` (TTS), `vllm` (LLM) are absent from `pod/requirements.lock`
    and `deploy/docker/pod.Dockerfile`. The built image can only run MOCK
    stages. A "real-stage smoke" needs these deps + model weights.
  - **No RunPod network volume**: `GET /v2/network-volumes` →
    `{"networkVolumes":[]}`. Real stages mount weights at
    `/workspace/models` (VOLUME.md layout); nothing exists to mount.
  - **No container registry with the image**: repo `dominator509/vihs` does
    not exist on GitHub (verified via gh) → no GHCR target; no Docker Hub
    creds. RunPod cannot pull `vihs-pod:478a6f6` from anywhere yet.
  - Orchestrator public reachability is fine: binds `0.0.0.0:8080` on public
    IP 66.94.123.250.
  - Deploy-side artifacts that ARE unblocked are committed (76d7bf4): RunPod
    template.json, VOLUME.md layout, systemd units, INSTALL.md.
## 15. Outcomes & Retrospective
M1–M3 complete (`ab817d5`, `e5bffb9`); M4 deploy artifacts committed
(`76d7bf4`, `c51c287`) — RunPod template, VOLUME.md, systemd units, INSTALL.md,
honest blocker log. Registry unblocked: `dominator509/vihs` private repo created
via gh (same pattern as the private axiom repo), pod image pushed to GHCR
(`ghcr.io/dominator509/vihs/vihs-pod:478a6f6`, digest `a9ba6b3b…`).
M4 remaining (needs operator decisions, see Surprises):
1. Real-stage smoke requires model weights + real-stage deps — the built image
   has mock stages only (faster_whisper/piper/vllm absent from lock+Dockerfile),
   and `GET /v2/network-volumes` returns `[]`. Which models / sizes to provision,
   and does the operator supply them?
2. smoke harness remote mode — `--base-url` is accept+ignore; wiring it to
   target a remote orchestrator + remote pod is in-scope M4 code.
3. Staging pod addressing — driver does not set VIHS_POD_ADDR; a RunPod pod must
   advertise a publicly reachable addr (via VIHS_RUNPOD_ENV post-create), or the
   orchestrator's pod-ward signal WS cannot connect. Deploy script needs the
   create → read runtime IP → set env → restart cycle.
STOP S1 holds on model weights until decided.

## 16. M4 completion (EP-009, verified 2026-08-10)
M4 STAGING is DONE — `verify.sh` VERIFY OK (all 12 sections) and the real
remote smoke GREEN on a live RunPod 4090 pod:
- `deploy/runpod/staging-deploy.py` creates ONE pod (ttl.sh slim launcher,
  ctranslate2 whisper + piper voices + cublas staged on volume `0z0kdx56tb`),
  waits Ready, runs the remote smoke through the real relay, and ALWAYS
  terminates the pod in `finally` (operator hard rule: no held-open billing).
- Smoke: cold_start 73.9s; `client WebRTC connected through relay (remote
  pod)`; `turn 1 committed (user + assistant in transcript)`; `remote resume:
  both turns present in order; transcript durable`; `e2e_remote_resume OK`;
  deploy exit 0; pod terminated; 0 pods after.
- Two root causes found and fixed along the way:
  1. Renderer `persona_name()` picked the session-create bootstrap note
     (`"session created"`, role=system, meta.owner) as the speaker label —
     every assistant utterance rendered as `**session created**` so the
     smoke's `**Assistant**` needle could never match. The pipeline was
     committing correctly all along (pod log-forwarding proved events POST
     200). Fix: `Event::Meta.owner` + skip owner-bound notes in
     `persona_name()` (goldens unchanged — persona notes carry no owner).
  2. rustls panicked at first TLS use ("Could not automatically determine
     CryptoProvider"): reqwest pulls ring, tokio-tungstenite pulls
     aws-lc-rs. Fix: direct rustls dep (ring) + `install_default()` at
     orchestrator start.
- Also shipped: piper persistent-process fix (per-clause model reload
  killed; local 21.9s→4.7s), orchestrator wss relay scheme mapping,
  slim launcher image + wheel mirror + log-forwarding (VIHS_LOG_POST),
  probe ladder (turn/c2/c3), check_status/check_volumes scripts.
- Remaining M5 (rollback drill) per EP-009 §5; P1–P8 drills in EP-010.
- Env notes: `.env` carries VIHS_RUNPOD_IMAGE=ttl.sh/vihs-pod-slimlauncher:24h,
  VIHS_ORCH_ADDR=66.94.123.250:8080 (public for RunPod pods; local chaos
  drills force 0.0.0.0 hermetic binds).

## 17. M5 completion (EP-009, verified 2026-08-10)
M5 ROLLBACK DRILL is DONE — `deploy/runpod/rollback-drill.py` exit 0, all
legs on REAL RunPod pods with release assertions:
- Tags created: `v0.1.0` (M4 state, ce9d335 — agent HARDCODES versions.pod
  "0.1.0") and `v0.2.0` (M5 state, bb8c1bc — agent reads VIHS_RELEASE).
  Images built from the tags and pushed: `ttl.sh/vihs-pod-slimlauncher:v0.1.0`
  and `:v0.2.0` (docker build from clean tag worktrees; digests differ).
- LEG 1 current v0.2.0: pod reports release v0.2.0, SMOKE_OK, cold_start
  54.8s, pod terminated.
- LEG 2 ROLLBACK to v0.1.0: pod reports release 0.1.0, SMOKE_OK —
  **rollback_s=97.8s** (budget 600s = EP-009 §5 validation ≤10 min), pod
  terminated.
- LEG 3 RESTORE v0.2.0: pod reports release v0.2.0, SMOKE_OK, pod terminated.
- Verification: 0 pods billing after the drill; volume 0z0kdx56tb intact.
- Release observability shipped: pod `versions.pod` from VIHS_RELEASE; GET
  /admin/pods exposes `versions`; run_e2e.py `--expect-release TAG`;
  smoke-test.sh `VIHS_EXPECT_RELEASE`; ENVIRONMENT.md row; RELEASE.md /
  DEPLOYMENT.md / ROLLBACK.md drill evidence; CHANGELOG entry.
- Real bugs found en route (both fixed, both logged in §13): deploy env merge
  order (.env would beat the drill override), orchestrator floor default on
  restart.
- EP-009 all milestones DONE. Next: EP-010 (P1–P8 production-readiness
  drills, launch ADR).

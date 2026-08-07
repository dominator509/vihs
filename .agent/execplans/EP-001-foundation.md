# EP-001 — Foundation (ACTIVE)

## 1. Purpose / Big Picture
Stand up the workspace skeleton so every later plan has a green baseline:
Rust workspace (vihs-core, memoryd, orchestrator), Python pod package, static
client stub, dev services, all scripts wired, CI, and `verify.sh` passing on
the empty skeleton. De-risks toolchain before any real logic exists.

## 2. Scope
Repo layout per ARCHITECTURE §2; toolchain pinning; format/lint/typecheck/test
harnesses for both languages; docker compose dev services; .env.example;
baseline CI workflow; all scripts in scripts/ implemented and green.

## 3. Non-goals
No domain logic (EP-002+). No real routes beyond /healthz stubs. No GPU/model
code. No provider drivers beyond an empty `mock` module. No client behavior
beyond a placeholder page.

## 4. Context and Orientation
Greenfield (EP-000 SKIPPED). ARCHITECTURE §2 is the layout contract;
COMMANDS.md defines what every script must do; ENVIRONMENT.md is the env
registry.

## 5. Files to Read First
AGENTS.md, COMMANDS.md, ARCHITECTURE.md §2–3, ENVIRONMENT.md, TESTING.md,
CONTRIBUTING.md.

## 6. Files to Change (create)
Cargo.toml (workspace), rust-toolchain.toml, .gitignore, .env.example,
crates/{vihs-core,memoryd,orchestrator}/{Cargo.toml,src/lib.rs|main.rs},
pod/{pyproject.toml,requirements.lock,vihs_pod/__init__.py,
vihs_pod/agent.py(stub),tests/test_smoke.py}, client/index.html (stub),
deploy/docker/compose.dev.yml, .github/workflows/ci.yml, scripts/* per
COMMANDS.md table.

## 7. Interfaces and Contracts
- Each service binary exposes GET /healthz returning 200 "ok" (axum) — the
  only route this plan builds.
- Scripts print their exact OK lines from COMMANDS.md and exit nonzero
  otherwise; every script passes `sh -n`.
- compose.dev.yml provides redis:7 on 6379 and minio on 9000/9001 and an
  idempotent bucket-create init step for `vihs-sessions`.

## 8. Milestones
M1 Workspace + toolchain.
  Goal: `cargo check --workspace` green with three crates; pinned toolchain.
  Read: ARCHITECTURE §2. Change: Cargo.toml, rust-toolchain.toml, crate stubs.
  Exact edits: workspace members list; each crate lib/main with healthz stub
  (orchestrator, memoryd) or empty lib (vihs-core); thiserror + serde_json +
  blake3 + tokio + axum deps declared where the later plans need them.
  Validation: `cargo check --workspace`. Expected: success, no warnings.
  Recovery: dependency resolution failure ⇒ pin exact versions from crates.io
  index in Cargo.toml; log versions in Decision Log.
M2 Python pod package.
  Goal: importable vihs_pod with pytest+mypy+ruff harness.
  Change: pod/pyproject.toml (ruff, mypy strict, pytest config),
  requirements.lock (pytest, mypy, ruff only at this stage), agent.py stub
  (`main()` prints "pod ready" with --mock-gpu flag parsed), test_smoke.py.
  Validation: `pod/.venv/bin/pytest pod -q` after install.sh. Expected: 1 passed.
M3 Scripts + dev services.
  Goal: every COMMANDS.md script exists, `sh -n` clean, dev services boot.
  Change: scripts/*.sh, deploy/docker/compose.dev.yml, .env.example.
  Exact behavior: scripts are thin, ordered wrappers (see scripts/ contents in
  this pack — copy then adapt paths); dev-services.sh up waits for
  redis PING and minio /minio/health/ready, creates bucket via `mc` container.
  Validation: `sh scripts/dev-services.sh up && sh scripts/preflight.sh`.
  Expected: DEV-SERVICES OK then PREFLIGHT OK.
  Recovery: port conflicts ⇒ document override vars in ENVIRONMENT.md
  (COMPOSE ports), Decision Log entry.
M4 CI + verify baseline.
  Goal: verify.sh green locally and in CI on the skeleton.
  Change: .github/workflows/ci.yml (jobs: rust, python, scripts — runs
  verify.sh with dev services as CI services).
  Validation: `sh scripts/verify.sh`. Expected: VERIFY OK.

## 9. Concrete Steps
Follow milestone order; commit per milestone (`EP-001 M<n>: …`). Any
placeholder that must survive this plan is marked `# EP-00X will replace`.

## 10. Validation and Acceptance
Acceptance: verify.sh VERIFY OK on skeleton; healthz curl returns ok for both
services started per COMMANDS.md; layout matches ARCHITECTURE §2 exactly;
CI workflow present and referencing only COMMANDS.md scripts.

## 11. Idempotence and Recovery
All steps re-runnable. install.sh recreates venv when missing;
dev-services.sh up is idempotent. If interrupted, rerun the last milestone's
validation to locate state.

## 12. Progress
- [x] M1 workspace  - [ ] M2 pod pkg  - [ ] M3 scripts+services  - [ ] M4 CI+verify

## 13. Surprises & Discoveries
## 14. Decision Log
## 15. Outcomes & Retrospective

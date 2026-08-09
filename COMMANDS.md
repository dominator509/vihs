# COMMANDS.md — Canonical Command Registry (VIHS)

Coding agents must not invent commands. If a command is missing, update this
file first with evidence from the repository.

Working directory rule: every command runs from the repository root unless the
row says otherwise. Package managers: `cargo` (Rust workspace), `pip` inside
`pod/.venv` (Python pod agent). No npm/yarn/pnpm anywhere.

| Purpose | Command | Expected success signal |
|---|---|---|
| Preflight | `sh scripts/preflight.sh` | `PREFLIGHT OK` |
| Install (all) | `sh scripts/install.sh` | `INSTALL OK` |
| Lint | `sh scripts/lint.sh` | `LINT OK` (clippy `-D warnings` + ruff clean) |
| Format check | `sh scripts/format-check.sh` | `FORMAT OK` (`cargo fmt --check`, `ruff format --check pod`) |
| Typecheck | `sh scripts/typecheck.sh` | `TYPECHECK OK` (`cargo check --workspace`, `mypy pod/vihs_pod`) |
| Unit tests | `sh scripts/test-unit.sh` | `UNIT OK` |
| Integration tests | `sh scripts/test-integration.sh` | `INTEGRATION OK` (needs dev services) |
| E2E tests | `sh scripts/test-e2e.sh` | `E2E OK` (single-pod loopback, mock GPU stages) |
| Chaos suite | `sh scripts/chaos.sh` (EP-007) | `CHAOS OK` (kill_pod_midturn, memoryd_pause, redis_loss_rebuild, stage_crash, torn_write_fsck) |
| Build | `sh scripts/build.sh` | `BUILD OK` (release binaries + pod wheel + docker image if DOCKER=1) |
| Security check | `sh scripts/security-check.sh` | `SECURITY OK` (secret scan + redaction test) |
| Dependency audit | `sh scripts/dependency-audit.sh` | `AUDIT OK` (`cargo audit`, `pip-audit`) |
| Smoke test | `sh scripts/smoke-test.sh` | `SMOKE OK` (boot stack, one scripted turn, resume) |
| Full verification | `sh scripts/verify.sh` | `VERIFY OK` (runs all of the above in order) |
| Production readiness | `sh scripts/production-readiness-check.sh` | `PROD-READY OK` |
| Local dev services | `sh scripts/dev-services.sh up` / `down` | `DEV-SERVICES OK` (Redis 7 + MinIO via docker compose) |
| Observability stack (dev) | `sh scripts/dev-services.sh up-obs` / `down-obs` | prometheus :9090 + grafana :3100 (EP-008 M3); waits for both health endpoints |
| Local start (control plane) | `cargo run -p orchestrator` and `cargo run -p memoryd` | listening logs on :8080 / :8091 |
| Local start (pod, mock stages) | `pod/.venv/bin/python -m vihs_pod.agent --mock-gpu` | `pod ready` log |
| Chain fsck (event-log integrity) | `cargo run -p vihs-core --bin chain-fsck -- <events.jsonl>` | `CHAIN OK <n> events` |
| Capacity load test | `sh scripts/loadtest-capacity.sh` (added in EP-007) | prints derived `sessions_per_gpu` |

Migration commands: not applicable (no relational DB). Schema evolution =
event `v` field bump + reader tolerance; see SPEC-002.

## Forbidden commands
- Any `rm`/`aws s3 rm`/`mc rm` against `sessions/` prefixes (see AGENTS.md §13).
- `git push --force` on shared branches; `git clean -fdx` (destroys .env, venv).
- Any command targeting a non-local Redis/S3/RunPod endpoint outside deploy
  scripts (STOP S6).
- Package-manager global installs (`pip install` outside `pod/.venv`, `cargo
  install` of unpinned tools not listed in ENVIRONMENT.md).

## Recovery instructions
- Dev services wedged: `sh scripts/dev-services.sh down && sh scripts/dev-services.sh up`.
- Rust build cache corrupt: `cargo clean -p <crate>` (never full `cargo clean`
  first — it costs minutes; escalate only on second failure).
- Python venv broken: remove `pod/.venv`, rerun `sh scripts/install.sh`.
- MinIO bucket missing: `sh scripts/dev-services.sh up` recreates
  `vihs-sessions` idempotently.
- A validation script exits nonzero without its OK line: treat as failure even
  if sub-tools looked green; scripts are the source of truth.

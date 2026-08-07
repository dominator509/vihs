# Checklist: Preflight (mirror of scripts/preflight.sh, human-readable)
- [ ] Repo clean or intentionally dirty (`git status` reviewed).
- [ ] Toolchains present at pinned versions (cargo, python3.11, docker, jq).
- [ ] `sh scripts/install.sh` run; pod/.venv healthy.
- [ ] Dev services up and reachable (redis PING, minio ready, bucket exists).
- [ ] .env present; required vars from ENVIRONMENT.md validate.
- [ ] Test harnesses runnable (cargo test --no-run; pytest --collect-only).
- [ ] Required secrets for THIS plan present, or plan doesn't need them.
- [ ] No known blockers in active ExecPlan Surprises & Discoveries.

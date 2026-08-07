# CONTRIBUTING.md — VIHS (humans and coding agents)

## Setup
scripts/install.sh → scripts/dev-services.sh up → cp .env.example .env →
scripts/preflight.sh. See ENVIRONMENT.md.

## Branch rules
Trunk-based; branch `ep-NNN-short-desc` for ExecPlan work; PR to main; main
must stay green.

## Coding standards
Rust: rustfmt defaults, clippy `-D warnings`, no `unsafe` without ADR, errors
via `thiserror` types per crate, no `unwrap()` outside tests. Python (pod):
ruff + ruff-format, mypy strict on `vihs_pod`, asyncio-only concurrency (no
threads except where a runtime demands it, then documented), stage interfaces
are `typing.Protocol`s. Shell: POSIX sh, `set -eu`, verified with `sh -n`.
Client JS: no build step, ES modules, no external CDNs.

## Test requirements
TESTING.md governs. Behavior lands with its tests in the same PR/milestone.

## Documentation requirements
AGENTS.md §11: specs, ENVIRONMENT, COMMANDS, DECISIONS updated in the same
change that makes them stale.

## Commit guidance
Imperative subject ≤72 chars, body says WHY, reference EP/SPEC ids
(`EP-003 M2: idempotent append dedup window`).

## Pull request checklist
[ ] Scope matches one ExecPlan/milestone. [ ] verify.sh green locally.
[ ] Diff matches Expected Changed Files. [ ] Docs updated. [ ] No secrets.

## Code review checklist
Invariants untouched or ADR'd; layer/import rules hold; input validated at
new edges; tests assert behavior; redaction respected; latency-path changes
carry histogram evidence.

## Agent-specific rules
Everything in AGENTS.md binds. One active ExecPlan; continue by default; STOP
conditions only; final response format §15.

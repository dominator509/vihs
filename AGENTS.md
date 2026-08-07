# AGENTS.md — Control Plane for Coding Agents (VIHS)

Do not ask the user for next steps. Proceed autonomously through the active
ExecPlan unless a STOP condition applies.

## 1. Mission
Implement VIHS — a real-time, streamed, barge-in-capable avatar pipeline with
pod-independent resumable session memory — exactly as specified by the specs
and ExecPlans in `.agent/`, to the production-readiness bar in
PRODUCTION_READINESS.md, without scope drift.

## 2. Source-of-truth priority
1. Current explicit user instruction.
2. This AGENTS.md.
3. The active ExecPlan (`.agent/execplans/EP-*.md` marked ACTIVE in `.agent/PLANS.md`).
4. Existing repository code and tests.
5. ARCHITECTURE.md.
6. The relevant `.agent/specs/SPEC-*.md`.
7. ROADMAP.md (never implement from it directly).

On conflict, higher wins. Record the conflict and resolution in the ExecPlan
Decision Log.

## 3. Required workflow
1. Read AGENTS.md (this file). 2. Read COMMANDS.md. 3. Read `.agent/PLANS.md`
and the active ExecPlan in full. 4. Run `scripts/preflight.sh`. 5. Complete
milestones strictly in order. 6. Run the milestone's validation command; compare
to expected result. 7. Update the ExecPlan Progress checkboxes and logs.
8. Continue autonomously to the next milestone. 9. Stop only under §4.
10. Finish with the Final Response format in §15.

## 4. STOP conditions (the only reasons to stop)
- S1: A required secret, credential, paid service, or external account is
  missing (e.g. RunPod API key, model weights path) and no mock path is defined.
- S2: An action may destroy user or production data (session event logs are
  user data; never delete outside the tested hard-delete path).
- S3: A legal, security, or financial judgment is required that no spec answers.
- S4: A materially different user-visible behavior choice is not resolved by
  any spec (e.g. what the avatar says on resume).
- S5: Required tests cannot run after the documented recovery attempts in §7.
- S6: Production deployment or an irreversible migration would occur without
  explicit permission.
When stopping, report: exact blocker, evidence (file/terminal output), the
smallest decision needed, and a recommended default.

## 5. Anti-drift rules
- Implement only what the active ExecPlan scopes. Non-goals are forbidden work.
- No broad refactors, styling rewrites, dependency swaps, file moves, or
  unrelated cleanup unless the active ExecPlan requires them.
- Before finishing, run `git diff --name-only` and compare against the
  ExecPlan's Expected Changed Files. Justify every extra file in the Decision
  Log or revert it.
- Never implement directly from ROADMAP.md.

## 6. Anti-hallucination rules
- Do not invent package APIs, command names, environment variables, database
  keys, Redis key shapes, routes, or config keys. The canonical registries are:
  routes → SPEC-003; Redis/object-store keys → SPEC-002; env vars →
  ENVIRONMENT.md; commands → COMMANDS.md.
- Confirm every name by reading the repository file that defines it before use.
- Use only commands from COMMANDS.md. If one is missing or stale, update
  COMMANDS.md first with evidence from the repository, then use it.
- Record every assumption in the ExecPlan Decision Log and ASSUMPTIONS.md.

## 7. Anti-fixation rules (bounded retry)
For any failing validation command, same-root-cause counting:
1. First failure: read the full error, identify the likely cause, make the
   smallest targeted fix, rerun the narrowest relevant command.
2. Second failure: write or run a narrower diagnostic (single test, single
   crate, `cargo test -p <crate> <name>`, `pytest -k <name>`), isolate it.
3. Third failure: stop that approach entirely. Record failed hypotheses in
   Surprises & Discoveries. Choose the simpler implementation path the ExecPlan
   or spec names as fallback. Continue if safe; otherwise STOP S5.
Never patch blindly around the same error. A new attempt requires a new
written hypothesis.

## 8. Dependency rules
- Check existing dependencies first (`Cargo.toml` workspace, `pod/pyproject.toml`).
- Prefer implementing with what exists. Add a dependency only when necessary,
  pinned, and recorded in the Decision Log + DECISIONS.md if architectural.
- Rust: no `unsafe` without an ADR. Python (pod only): pinned versions in
  `pod/requirements.lock`. No Node/npm anywhere in the build chain.

## 9. File creation rules
- Follow the repository map in ARCHITECTURE.md. New Rust code goes in the
  correct crate; new Python code under `pod/vihs_pod/`; client assets under
  `client/`; scripts under `scripts/` as POSIX sh verified with `sh -n`.
- Every new file appears in the ExecPlan's Expected Changed Files (or Decision
  Log justification).

## 10. Testing rules
- No milestone is complete until its validation command passes with the
  expected result. TESTING.md defines the pyramid and per-feature required
  tests. New behavior ⇒ new test in the same milestone, not later.
- Invariant tests (`chain_fsck`, prefix-stability, barge-in INV-1) are
  regression tests: they may never be deleted or weakened, only extended.

## 11. Documentation update rules
- Behavior changes update the owning SPEC in the same ExecPlan.
- New env var ⇒ ENVIRONMENT.md row. New command ⇒ COMMANDS.md + script.
- Architectural decisions ⇒ DECISIONS.md ADR entry.

## 12. Security rules
- Never commit secrets. `.env` is gitignored; `.env.example` holds placeholders.
- Logs must pass the redaction rules in OBSERVABILITY.md (no transcript text,
  no tokens, no session owner identifiers at info level).
- All input at trust boundaries (WebSocket signaling, HTTP API, memoryd append)
  is validated per SPEC-006 before use.
- `session_id` alone never authorizes anything (SPEC-005).

## 13. Production data rules
- There is no shared production environment in this repo's tests. Integration
  tests run against disposable local Redis/MinIO started by
  `scripts/dev-services.sh`. Any command targeting a non-local endpoint is
  forbidden without STOP S6 clearance.
- Session event logs are append-only. The only deletion path is the hard-delete
  API implemented and tested in EP-006. Manual deletion of `sessions/` objects
  is forbidden.

## 14. Definition of done (per ExecPlan)
- All acceptance criteria pass. All required validation commands pass with
  expected results. ExecPlan Progress fully updated. Final diff reviewed; only
  expected files changed. Remaining risks documented in Outcomes &
  Retrospective.

## 15. Final response requirements
Every completed ExecPlan run reports: ExecPlan completed; changed files;
commands run; command results; acceptance criteria status; decisions made;
assumptions confirmed or changed; remaining risks; whether
production-readiness criteria (if in scope) passed.

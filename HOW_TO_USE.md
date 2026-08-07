# How to Use This Blueprint Pack (VIHS)

## 1. Place files into the repository
Copy the entire pack contents (root docs, `.agent/`, `scripts/`) into the
root of a fresh git repository. `git add -A && git commit -m "VIHS blueprint
pack"`. The pack IS the repository's operating system; source code grows
around it under the layout in ARCHITECTURE.md §2.

## 2. Choose the active ExecPlan
`.agent/PLANS.md` tracks status. This pack ships with EP-000 SKIPPED
(greenfield) and EP-001 ACTIVE. Work plans strictly in ROADMAP order; flip
statuses in PLANS.md as they complete. Exactly one ACTIVE at a time.

## 3. Run preflight
`sh scripts/preflight.sh` — it distinguishes SKIP (component not built yet;
expected pre-EP-001) from FAIL (broken toolchain/config). Fix FAILs before
any implementation.

## 4. Run a lower-tier coding LLM against an ExecPlan
Generic invocation prompt (paste into any coding agent that can read files,
edit files, and run terminal commands):

    Read AGENTS.md, COMMANDS.md, .agent/PLANS.md, and [EXECPLAN_PATH].
    Implement [EXECPLAN_PATH] to completion.
    Do not ask for next steps.
    Do not implement from ROADMAP.md directly.
    Do not broaden scope.
    Complete milestones in order.
    Validate after each milestone.
    Update the ExecPlan as you work.
    Use only commands from COMMANDS.md.
    Stop only for STOP conditions in AGENTS.md.
    At the end, run the required verification command, run
    git diff --name-only, update Outcomes & Retrospective, and report
    changed files, commands run, results, decisions, risks, and
    acceptance status.

Codex-style example:

    codex --cd . \
      --ask-for-approval never \
      --sandbox workspace-write \
      "Read AGENTS.md, COMMANDS.md, .agent/PLANS.md, and .agent/execplans/EP-001-foundation.md. Implement EP-001-foundation.md to completion. Do not ask for next steps. Stop only for STOP conditions in AGENTS.md. Update the ExecPlan as you work. Run validation after each milestone."

If your runner lacks those flags, the same instruction pastes into any
file-reading, file-editing, command-running coding agent. The reusable
prompt variants live in `.agent/prompts/`.

## 5. Continue a partially completed plan
Use `.agent/prompts/continue-execplan.md`: the agent inspects Progress,
Surprises & Discoveries, and the Decision Log, re-runs the last completed
milestone's validation to confirm reality, then resumes at the first
incomplete milestone.

## 6. Debug failing validation
Use `.agent/prompts/debug-validation-failure.md`. The bounded-retry ladder
(AGENTS.md §7) is mandatory: smallest fix → narrower diagnostic → after a
third same-root failure, switch to the named simpler path. No blind patching.

## 7. Perform final review
Use `.agent/prompts/final-review.md` + `.agent/checklists/final-review.md`:
verify.sh, diff vs Expected Changed Files, acceptance criteria against the
RUNNING system, docs updated, status flipped, §15 report produced.

## 8. Decide production readiness
`sh scripts/production-readiness-check.sh` covers the automated subset;
PRODUCTION_READINESS.md is the full gate including human-witnessed drills
(chaos, restore, rollback). Launch requires the operator sign-off ADR;
production deploys are never agent-executed (STOP S6).

## 9. Avoid roadmap-only implementation
ROADMAP.md is strategy. If work seems needed that no ExecPlan covers, the
correct move is to write/extend an ExecPlan from the template — never to
code from the roadmap. AGENTS.md and EXECUTION_RULES.md both enforce this.

## 10. Update plans as the repository evolves
Reality outranks stale plan text (source-of-truth §2 order): correct the
plan, log the correction in Surprises & Discoveries, keep going. New
features: spec first, ExecPlan second, code third (ARCHITECTURE §14).
Keep COMMANDS.md, ENVIRONMENT.md, and the SPEC registries truthful in the
same change that would make them stale — those files are what keep
lower-tier agents from guessing.

## Pack inventory
17 root docs · .agent/: PLANS, EXECUTION_RULES, 4 prompts, 9 specs,
11 ExecPlans, 9 checklists, 5 templates · 16 scripts (POSIX sh,
syntax-verified). Reference implementations for the hardest pieces are
embedded where an implementing agent will read them: hash chain +
canonicalizer + deterministic render (ARCHITECTURE §7.2, EP-002),
single-writer idempotent append + crash-order recovery + compaction
(ARCHITECTURE §7.3, EP-003), autoscaler pure-policy + router cap
enforcement (EP-004), endpointing FSM + clause chunker + AbortBus/INV-1
ledger + prefix-stable context (SPEC-001, EP-005).

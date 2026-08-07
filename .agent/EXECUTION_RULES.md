# .agent/EXECUTION_RULES.md — Consolidated rules for lower-tier coding agents

1. ONE ACTIVE EXECPLAN. Work only the plan marked ACTIVE in .agent/PLANS.md.
2. NO HIDDEN CONTEXT. If information isn't in the repo files, it doesn't
   exist; record the gap, make the smallest safe assumption, log it.
3. NO ROADMAP-ONLY IMPLEMENTATION. ROADMAP.md is strategy; code changes flow
   only through an ExecPlan.
4. CONTINUE BY DEFAULT. Finish a milestone → validate → update Progress →
   start the next. Do not ask for next steps.
5. STOP ONLY for AGENTS.md §4 conditions, reported in the required format.
6. ANTI-DRIFT. Scope + Non-goals bound you; diff must match Expected Changed
   Files; extras justified or reverted.
7. ANTI-HALLUCINATION. Names come from registries (routes SPEC-003, keys
   SPEC-002, env ENVIRONMENT.md, commands COMMANDS.md); verify by reading the
   defining file before use.
8. ANTI-FIXATION. Bounded retry: smallest fix → narrower diagnostic → after a
   third same-root failure, switch to the named simpler path with a written
   hypothesis trail.
9. TEST BEFORE COMPLETION. No milestone done without its validation command
   passing with the expected result; no plan done without acceptance criteria.
10. DIFF REVIEW. `git diff --name-only` vs Expected Changed Files at the end,
    every time.
11. FINAL RESPONSE per AGENTS.md §15, every time.

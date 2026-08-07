# Prompt: Final Review of an ExecPlan

Read AGENTS.md, the completed ExecPlan, and .agent/checklists/final-review.md.

Run `sh scripts/verify.sh`. If the plan is EP-010 (or states it), also run
`sh scripts/production-readiness-check.sh`. Run `git diff --name-only`
against the merge base and compare with the plan's Expected Changed Files;
justify or revert every discrepancy.

Verify each acceptance criterion against the running system, not the code's
intent. Confirm docs the change made stale were updated (specs, ENVIRONMENT,
COMMANDS, DECISIONS). Confirm no secrets in the diff and no forbidden paths
touched.

Update Outcomes & Retrospective with: what shipped, deviations, risks left,
follow-ups. Flip the plan status in .agent/PLANS.md. Produce the final report
per AGENTS.md §15.

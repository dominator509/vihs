# Prompt: Execute Active ExecPlan

Read AGENTS.md, COMMANDS.md, .agent/PLANS.md, and [EXECPLAN_PATH].
Optional user request to honor within scope: [OPTIONAL_USER_REQUEST]

Then: run `sh scripts/preflight.sh`; implement the milestones of
[EXECPLAN_PATH] strictly in order; after each milestone run its validation
command and confirm the expected result; update the ExecPlan's Progress,
Surprises & Discoveries, and Decision Log as you work; continue autonomously.

Do not ask for next steps. Do not implement from ROADMAP.md. Do not broaden
scope beyond the plan's Scope/Non-goals. Use only commands from COMMANDS.md.
Stop only under the STOP conditions in AGENTS.md §4, reporting blocker,
evidence, smallest decision needed, and recommended default.

At the end: run `sh scripts/verify.sh` (or the plan's stated final command),
run `git diff --name-only` and reconcile against Expected Changed Files,
update Outcomes & Retrospective, flip the plan's status in .agent/PLANS.md,
and produce the final response required by AGENTS.md §15.

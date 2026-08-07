# Prompt: Continue a Partially Completed ExecPlan

Read AGENTS.md, COMMANDS.md, .agent/PLANS.md, and [EXECPLAN_PATH].

Before writing any code: inspect the ExecPlan's Progress checkboxes,
Surprises & Discoveries, and Decision Log to learn true current state; then
verify reality matches — re-run the validation commands of the last milestone
marked complete (all validation commands are safe to re-run). If reality
disagrees with Progress, trust reality, correct the checkboxes, and note it
in Surprises & Discoveries.

Resume at the first incomplete milestone. Re-validate any prior assumption
the Decision Log marks unverified. Continue autonomously under
.agent/EXECUTION_RULES.md; stop only for AGENTS.md §4 STOP conditions. Finish
with the AGENTS.md §15 final response.

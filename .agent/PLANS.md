# .agent/PLANS.md — ExecPlan Standard (VIHS)

An ExecPlan is a self-contained implementation document for one feature or
system change. A new agent with no prior conversation must be able to
continue from the ExecPlan alone.

## Active plan
Exactly one ExecPlan is ACTIVE at a time. Current: **EP-007 (ACTIVE)** —
EP-006 is DONE. Update this line when a plan completes; the next plan in
ROADMAP order becomes ACTIVE unless the user says otherwise.

Status legend: PENDING → ACTIVE → DONE (or SKIPPED with recorded reason).
| EP | Status | | EP | Status |
|---|---|---|---|---|
| 000 | SKIPPED (greenfield) | | 006 | DONE |
| 001 | DONE | | 007 | ACTIVE |
| 002 | DONE | | 008 | PENDING |
| 003 | DONE | | 009 | PENDING |
| 004 | DONE | | 010 | PENDING |
| 005 | DONE | | | |

## Required sections (every ExecPlan, in order)
1 Purpose / Big Picture · 2 Scope · 3 Non-goals · 4 Context and Orientation ·
5 Files to Read First · 6 Files to Change · 7 Interfaces and Contracts ·
8 Milestones · 9 Concrete Steps · 10 Validation and Acceptance ·
11 Idempotence and Recovery · 12 Progress · 13 Surprises & Discoveries ·
14 Decision Log · 15 Outcomes & Retrospective

## Execution rules
Work milestones strictly in order; one milestone fully validated before the
next begins. Use only COMMANDS.md commands. Anti-drift/hallucination/fixation
rules from AGENTS.md apply verbatim.

## Milestone rules
Every milestone has: goal, files to read, files to change, exact edits
expected, validation command, expected result, recovery instruction. A
milestone without all seven is not runnable — fix the plan first (that fix is
in-scope, log it).

## Validation & acceptance rules
Validation commands are the milestone gate; the expected result is literal
(the script's OK line or a named test set passing). Plan-level acceptance
criteria are observable behaviors, checked at the end against the running
code, not against intentions.

## Idempotence & recovery rules
Every plan states how to resume after interruption at any milestone: what to
inspect to learn current state, what is safe to re-run (all validation
commands are), and what must not be repeated blindly.

## Progress / logs rules
Progress checkboxes updated as work happens, not retroactively. Surprises &
Discoveries gets anything that contradicted the plan. Decision Log gets every
choice the plan didn't pre-make (smallest reversible option + why).

## Completion rules
DONE = AGENTS.md §14 definition of done + this plan's acceptance criteria +
status flipped here in PLANS.md + final response per AGENTS.md §15.

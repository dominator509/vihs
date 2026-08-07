# EP-000 — Repository Discovery

## 1. Purpose / Big Picture
Establish ground truth about the repository before any other plan runs. For
existing or unknown repositories this plan is MANDATORY. For a confirmed
greenfield repository it is SKIPPED via the gate in Milestone M1.

## 2. Scope
Read-only inventory of the repo, toolchain, commands, CI, environment; update
COMMANDS.md, ARCHITECTURE.md (repo map "current" state), ASSUMPTIONS.md.

## 3. Non-goals
No code changes. No dependency installs beyond version probes. No fixes.

## 4. Context and Orientation
Inputs said Greenfield (ASSUMPTIONS A-01). This plan verifies that claim and
either exits SKIPPED or performs full discovery.

## 5. Files to Read First
AGENTS.md, COMMANDS.md, ASSUMPTIONS.md, ARCHITECTURE.md.

## 6. Files to Change
COMMANDS.md, ARCHITECTURE.md, ASSUMPTIONS.md, this file (logs), .agent/PLANS.md.

## 7. Interfaces and Contracts
None (read-only plan).

## 8–9. Milestones & Concrete Steps
M1 Greenfield gate.
  Goal: decide SKIP vs full discovery. Read: repo root. Change: this file,
  .agent/PLANS.md.
  Steps: run `git log --oneline | head` and `ls -A`. If the only content is
  this blueprint pack (root docs, .agent/, scripts/) and no source tree
  exists: mark SKIPPED (greenfield) in .agent/PLANS.md, paste evidence in
  Progress, END PLAN.
  Validation: `ls -A` output in Progress. Expected: blueprint files only.
  Recovery: if ambiguous, treat as existing repo, continue to M2.
M2 Inventory (existing repos only).
  Goal: complete manifest/entrypoint map. Steps: `find . -maxdepth 2 -type d`;
  read every manifest (Cargo.toml, pyproject.toml, package.json, Dockerfiles,
  CI configs); list entrypoints and test dirs.
  Validation: inventory table in Surprises & Discoveries. Recovery: unreadable
  file ⇒ note and continue.
M3 Command detection.
  Goal: COMMANDS.md matches reality. Steps: derive build/test/lint commands
  from manifests + CI; update rows citing evidence (file:line). Never guess;
  missing ⇒ NEEDS-CONFIRMATION placeholder.
  Validation: every changed row cites evidence.
M4 Architecture & risk capture.
  Goal: docs reflect current layout + risks. Steps: update ARCHITECTURE.md
  repo map with actual layout; divergences listed as risks; ASSUMPTIONS rows
  confirmed/refuted.
  Validation: `git diff --name-only` ⊆ {COMMANDS.md, ARCHITECTURE.md,
  ASSUMPTIONS.md, this file, .agent/PLANS.md}.

## 10. Validation and Acceptance
Acceptance: PLANS.md reflects SKIPPED with evidence, or discovery docs
updated with evidence; zero source files changed.

## 11. Idempotence and Recovery
Fully re-runnable; discovery overwrites its own prior findings.

## 12. Progress
- [ ] M1 gate decided (evidence pasted)
- [ ] M2 inventory (n/a if skipped)
- [ ] M3 commands (n/a if skipped)
- [ ] M4 architecture/risks (n/a if skipped)

## 13. Surprises & Discoveries
(record here)

## 14. Decision Log
(record here)

## 15. Outcomes & Retrospective
(record on completion)

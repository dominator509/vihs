# Checklist: Final Review (per ExecPlan)
- [ ] Every acceptance criterion verified against running code.
- [ ] sh scripts/verify.sh → VERIFY OK on the final tree.
- [ ] git diff --name-only compared to Expected Changed Files; extras
      justified in Decision Log or reverted.
- [ ] Docs made stale by the change were updated (specs/ENV/COMMANDS/ADR).
- [ ] No secrets in the diff (security-check green).
- [ ] No production data paths touched; no forbidden commands used.
- [ ] Remaining risks written into Outcomes & Retrospective.
- [ ] Plan status flipped in .agent/PLANS.md.
- [ ] Final response includes everything AGENTS.md §15 requires.

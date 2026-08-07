# Prompt: Debug a Failing Validation Command

Read AGENTS.md §7 (bounded retry), COMMANDS.md, and the active ExecPlan.

Rules: do not rewrite unrelated code; do not touch files outside the failing
area without logging why. Procedure:
1. Capture the exact failing command and the exact error output (paste into
   Surprises & Discoveries).
2. Form ONE written hypothesis for the root cause.
3. Make the smallest fix consistent with that hypothesis.
4. Re-run the NARROWEST command that exercises the fix (single test / single
   crate / -k selector), then the original failing command.
5. On a second same-root failure: build or run a narrower diagnostic to
   isolate (minimal repro test is ideal).
6. On a third same-root failure: STOP this approach — record all failed
   hypotheses, adopt the simpler fallback path named by the ExecPlan or spec,
   and continue if safe; otherwise raise STOP S5.
Update the ExecPlan logs before returning to normal execution.

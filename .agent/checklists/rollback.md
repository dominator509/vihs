# Checklist: Rollback
- [ ] Trigger confirmed against ROLLBACK.md list; decision owner (operator) engaged.
- [ ] Previous tag + image identified; commands staged.
- [ ] Method chosen: app rollback (default) / config / drain-limited.
- [ ] Data considerations reviewed: no data rollback exists; corrupt-event
      case → isolate sessions, never delete (chain evidence).
- [ ] Executed in order: memoryd → orchestrator → pod template pin.
- [ ] Verification: smoke green; dashboards at baseline 30 min; sample fsck.
- [ ] Communication posted (staging note / prod incident entry).
- [ ] Postmortem scheduled ≤48 h; regression test task created.

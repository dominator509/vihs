# Checklist: Release
- [ ] RC criteria met (verify green on tag; audit reviewed; invariants green).
- [ ] Version tagged vX.Y.Z; CHANGELOG.md entry complete.
- [ ] Artifacts + pod image built by CI from the tag.
- [ ] Staging deployed in order (memoryd → orchestrator → pod roll).
- [ ] Staging smoke SMOKE OK + 30-min soak clean.
- [ ] Operator approval recorded (prod is STOP S6 for agents).
- [ ] Production deployed; post-deploy smoke + verification green.
- [ ] 24 h monitoring window watched; rollback triggers understood.

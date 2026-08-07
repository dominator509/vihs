# Checklist: Incident Response
- [ ] DETECT: alert/report captured with timestamp + affected surface.
- [ ] TRIAGE: severity per OPERATIONS.md (SEV1 integrity / SEV2 availability
      / SEV3 latency); owner engaged for SEV1/2.
- [ ] MITIGATE: apply the matching OPERATIONS.md failure-mode action; never
      touch sessions/ objects manually.
- [ ] COMMUNICATE: status posted; user-visible note if >5 min impact.
- [ ] RESOLVE: root cause addressed or safely deferred with guard in place.
- [ ] VERIFY: smoke + dashboards + (if integrity) targeted chain-fsck.
- [ ] DOCUMENT: timeline + evidence into postmortem (48 h for SEV1/2).
- [ ] FOLLOW UP: regression test + action items filed (test-first rule).

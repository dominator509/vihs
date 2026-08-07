# EP-006 — Auth, Security, Permissions

## 1. Purpose / Big Picture
Replace the permissive dev Authorizer with the real thing: opaque tokens,
ownership gating, pod-token scoping, signed-URL discipline, rate limits,
redaction enforcement, hard-delete/TTL exposure, audit lines. After this
plan, T1–T6 in SECURITY.md have their controls implemented and tested.

## 2. Scope
Token store + mint/verify (argon2id+pepper, constant-time), Authorizer impls
in orchestrator and memoryd behind the existing traits, A1–A8 from SPEC-005,
rate limiting, 404-not-403 convention, redaction middleware + tests, audit
logging, security-check.sh made real (secret scan + redaction tests), client
token handling per SPEC-004 F1.

## 3. Non-goals
OIDC (ADR-006 v2). App-layer envelope encryption (ADR-008). WAF/infra TLS
(deploy docs). New routes beyond POST /admin/tokens (already in SPEC-003).

## 4–5. Context / Files to Read First
SPEC-005 (normative), SECURITY.md, SPEC-003 (authz column per route),
SPEC-006 (401/404/403/429 mapping), OBSERVABILITY.md redaction classes.

## 6. Files to Change
crates/orchestrator/src/{authz.rs,tokens.rs,ratelimit.rs,audit.rs},
crates/memoryd/src/authz.rs, both services' log middleware, pod
memory_client (send pod token), client/session.js (F1 memory-only storage +
opt-in), scripts/security-check.sh, tests: authz matrix (generated FROM the
SPEC-003 registry table — parse the markdown table in the test so new routes
fail closed), pod-token boundary, expiry/revocation, rate limits, redaction,
signed-URL expiry, audit-line shape.

## 7. Interfaces and Contracts
`Authorizer::allow(token, sid, verb) -> Principal` (existing seam). Token
record per SPEC-005 model in Redis `token:{token_id}`. Rate limiter:
fixed-window per token per class (create/resume/signal) with limits from
SPEC-005 A5, returning SPEC-006 `rate_limited`.

## 8. Milestones
M1 Token store + mint/verify + admin mint route. Validation: token unit tests
   (constant-time verify called, expiry, revocation) pass.
M2 Orchestrator authz on every route + 404 convention + matrix test.
   Validation: `cargo test -p orchestrator authz_matrix`. Expected: every
   session-scoped registry row covered owner/foreign/none.
M3 memoryd authz: pod token bound to one session + allowed verbs; signed-URL
   TTL test. Validation: `cargo test -p memoryd authz`.
M4 Rate limits + audit + redaction middleware + tests both services.
   Validation: `sh scripts/security-check.sh` SECURITY OK.
M5 Client F1 token handling + E2E auth path. Validation: `sh
   scripts/test-e2e.sh` E2E OK (now with real tokens minted by harness).

## 9. Concrete Steps
Milestone order; pepper from env (STOP S1 if unset in stage/prod configs —
dev generates ephemeral with loud log).

## 10. Validation and Acceptance
verify.sh green; acceptance = SPEC-005 acceptance criteria; SECURITY.md
checklist rows all checkable.

## 11. Idempotence and Recovery
Token tests namespace token_ids; revocation tests clean up. Re-runnable.

## 12. Progress
- [ ] M1 tokens  - [ ] M2 orch matrix  - [ ] M3 memoryd/pod scope
- [ ] M4 limits+redaction  - [ ] M5 client+e2e

## 13. Surprises & Discoveries
## 14. Decision Log
## 15. Outcomes & Retrospective

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

M1 actual changes (ratelimit.rs/audit.rs/client deferred to M2–M5; extra
files justified in Decision Log):
crates/orchestrator/src/{tokens.rs(NEW),authz.rs,api_admin.rs,api_internal.rs,
api_public.rs,lib.rs,main.rs}; Cargo.toml (+argon2/base64/rand/redis);
tests/{contract_public,integ_assign,integ_resume_flow}.rs; mcpd
tests/contract_mcp.rs; pod/requirements.lock (pytest-asyncio pin);
tests/e2e/run_e2e.py (bootstrap+mint); ENVIRONMENT.md, .env.example.

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
- [x] M1 tokens  - [x] M2 orch matrix  - [ ] M3 memoryd/pod scope
- [ ] M4 limits+redaction  - [ ] M5 client+e2e

M2 notes (validation `cargo test -p orchestrator --test authz_matrix` = 3 green;
workspace 97 green; clippy 0; fmt clean; VERIFY OK):
- authz_matrix.rs: route list PARSED from SPEC-003 "Orchestrator public API"
  table (section-scoped so memoryd rows are excluded); every session-scoped
  row covered owner (not 401/404) / foreign (404, never 403) / none (401).
  New registry rows fail closed until covered.
- Signaling WS (/v1/signal/{id}): permissive empty-check replaced with real
  `authz.allow(Verb::Session)` (SPEC-005 A1).
- Assign WS (/internal/pods/{id}/assign): had NO auth — now requires
  `authz.allow(Verb::Pod)` on upgrade (SPEC-005 A3); pod sends its seeded
  token via header (agent.py `self.bearer`).
- Admin listener: user-token rejection covered for /admin/pods, /admin/scale,
  /admin/pods/{id}/drain (A6).

M1 notes (validation `cargo test --workspace` = 94 green; clippy 0; fmt clean):
- `tokens.rs`: Redis-backed TokenStore — 32B base64url opaque tokens
  (16B id prefix), argon2id(token+pepper) OWASP params, constant-time
  verify, TTL expiry, immediate revocation. 6 unit tests.
- `authz.rs`: async Authorizer trait + TokenAuthorizer (scope-vs-verb);
  PermissiveAuthorizer retained for routing tests only.
- `POST /admin/tokens` real mint replaces EP-004 UUID placeholder;
  admin-only (user token rejected — A6 test). Contract tests mint real
  tokens via the store.
- Bootstrap gap closed: env-provided credentials seeded at startup —
  `VIHS_POD_TOKEN` (pod scope) + `VIHS_ADMIN_TOKEN` (admin scope) must be
  32-byte base64url; store rejects plain strings loudly (seed unit test).
  ENVIRONMENT.md + .env.example updated.
- E2E harness now mints a real user token via `POST /admin/tokens` using
  the seeded admin token; pod uses the real seeded pod token. Full
  3-target E2E gate GREEN.

## 13. Surprises & Discoveries
- The assign WS was completely unauthenticated before M2 — any caller could
  open /internal/pods/{id}/assign and receive assign frames. The pod agent
  already sent its bearer header on that socket, so the fix was additive.
- The first matrix attempt parsed the WHOLE SPEC-003 file and picked up
  memoryd's `/v1/sessions/{id}/events` rows as orchestrator routes → the
  owner case 404'd. Fixed by section-scoping the parser to the
  "## Orchestrator public API" block.

- Wiring the strict TokenAuthorizer into `build_state` broke three seams at
  once: (1) no bootstrap admin token existed, so `POST /admin/tokens` was
  unreachable (chicken-and-egg); (2) the legacy `VIHS_POD_TOKEN` dev value
  (`dev-pod-token`, 13 chars) can never pass `TokenStore::verify` (requires
  32-byte base64url); (3) E2E harness hardcoded fake tokens. Fix: `seed()`
  method + startup seeding of both env tokens + harness mint flow.
- `seed()` validates the 32-byte base64url shape so a seeded token always
  survives `verify()` — plain-string env values now fail loudly at boot
  instead of silently 401-ing every pod request.
- PRE-EXISTING GAPS FOUND (not M1-introduced; fixed to keep verify.sh green):
  (1) `pod/requirements.lock` pinned `pytest==9.0.3` but `pytest-asyncio==1.0.0`
  requires `pytest<9` — pip-audit could never resolve, so dependency-audit
  FAILED since EP-005 M4. Fixed: `pytest-asyncio==1.4.0` (supports pytest 9).
  (2) `scripts/dependency-audit.sh` swallows cargo-audit's non-zero exit with
  `|| echo "NOTE cargo-audit not installed"` — 3 real RUSTSECs in
  rustls-webpki 0.101.7 (via aws-smithy → memoryd S3 client) are reported but
  never fail the gate. Fix deferred: upgrading aws-sdk is a dependency swap
  (AGENTS §5) — needs its own decision. Logged, not silently patched.
  (3) `scripts/smoke-test.sh` passes `--smoke` but the harness never
  implemented it — verify.sh's final gate failed since the harness existed.
  Fixed: `--smoke` maps to the e2e_resume target (boot, one turn, resume,
  delete per COMMANDS.md).

## 14. Decision Log
- M1: env-provided credential seeding (VIHS_POD_TOKEN + VIHS_ADMIN_TOKEN)
  is the dev bootstrap path for the admin mint route. Alternative (hardcoded
  bootstrap admin token) rejected: env-driven matches the existing pepper
  convention and keeps secrets out of source. `seed()` validates shape so
  invalid env values fail loudly at boot (STOP S1-adjacent) rather than
  401-ing every request silently.
- M1: E2E harness mint flow pulled forward from M5 (plan says M5 validates
  "real tokens minted by harness") because the strict authorizer lands in
  M1 and verify.sh gates E2E on every milestone — the harness cannot stay
  on fake tokens across M1–M4.
- M1 files beyond §6 (AGENTS §5): api_admin.rs/api_public.rs/api_internal.rs/
  main.rs are the call sites that must switch from permissive to the real
  authorizer — the strict impl cannot land without wiring them. lib.rs hosts
  the TokenStore in AppState + env seeding. tests/*.rs are the required
  owner/scope contract tests. run_e2e.py mints the harness token. These are
  all M1-necessary, not scope drift.

## 15. Outcomes & Retrospective

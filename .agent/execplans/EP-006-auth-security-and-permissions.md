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
- [x] M1 tokens  - [x] M2 orch matrix  - [x] M3 memoryd/pod scope
- [x] M4 limits+redaction  - [ ] M5 client+e2e

M4 notes (validation `sh scripts/security-check.sh` = SECURITY OK; workspace
130 green; clippy 0; fmt clean; VERIFY OK — preflight/lint/format/typecheck/
unit/integration/e2e(3-target)/build/security/dependency-audit/smoke all OK):
- NEW `vihs-core/src/redact.rs`: `owner_hash()` (blake3 prefix, 8 hex —
  OBSERVABILITY.md redaction) + `scrub_log_line()` (masks bearer tokens and
  AWS SigV4 presigned-URL creds X-Amz-Signature/-Credential/-Security-Token).
  7 `redaction_*`-named tests — the security-check.sh gate runs
  `cargo test --workspace redaction` and previously SKIPped because no test
  name matched; now 7 run in the gate.
- NEW `crates/orchestrator/src/ratelimit.rs`: fixed-window per (token_id,
  class) in-memory limiter — Create ≤10/min, Resume ≤30/min, Signal ≤50/s
  (SPEC-005 A5), over-limit → `OrchError::RateLimited` (429 `rate_limited`).
  Clock-injected (`check_at`) for tests; 7 unit tests. Keyed by
  `vihs_auth::token_id()` (16-byte base64url prefix) — never the raw token.
- NEW `vihs_auth::token_id()` pub helper — derives the stable lookup id
  from a raw token (rate-limit keying; the derive previously lived only
  inside TokenStore's private `redis_key`).
- Wiring: `create_session` → Create class; `connect_session` (resume AND
  connect) → Resume class; signal WS c2p pump → Signal class per client
  frame (rate_limited error frame + close; client sink shared between pumps
  via Arc<Mutex> so the error frame reaches the client). OrchError::retryable()
  now returns true for RateLimited (SPEC-006 R1 — it was false, a real bug).
- NEW `crates/orchestrator/src/audit.rs`: structured audit lines (ids only,
  owner hashed via owner_hash): `session_deleted` (orchestrator DELETE route
  AND memoryd durable delete — SPEC-005 A7) + `token_minted` (admin mint,
  raw token never logged). 4 audit-line-shape tests.
- Redaction middleware: ScrubWriter (tracing_subscriber MakeWriter applying
  `scrub_log_line` to every line) installed in BOTH services' main.rs —
  the log boundary scrubs tokens/signed URLs even if a future log site
  leaks; raw owner ids fixed at the source (memoryd authz.rs debug logs +
  api.rs create-path log now emit owner_hash, not the raw owner).
- Tests: `crates/orchestrator/tests/rate_limit.rs` — 3 integration tests:
  create 10→429 with `rate_limited` retryable=true, resume 30→429 (404s on
  owner check prove the limiter fires independently of route outcome),
  per-token budget isolation.
- Fixed along the way: LINT gate greps vihs-core/src for sibling crate names
  (memoryd/orchestrator) — a redact.rs test fixture string tripped it;
  renamed to a service-neutral line.

M3 notes (validation `cargo test -p memoryd authz` = 10 green; workspace
110 green; clippy 0; fmt clean; VERIFY OK):
- NEW crate `crates/vihs-auth` (Layer 0, ARCHITECTURE.md updated): the shared
  TokenStore (mint/seed/verify/revoke) + Scope/Principal/TokenError +
  POD_TOKEN_TTL (15 min). ONE implementation so orchestrator AND memoryd
  verify each other's tokens with ONE pepper (VIHS_TOKEN_PEPPER). 7 unit
  tests incl. cross-store pepper mismatch regression.
- Orchestrator: tokens.rs/authz.rs re-export vihs-auth (error bridge
  TokenError→OrchError). `router::assign` mints a REAL session-bound pod
  token (owner=session_id, scope Pod, 15-min TTL) instead of the bare-UUID
  `pod_token()` helper (removed). The pod agent already consumed the frame's
  pod_token for memoryd calls — it now carries a verifiable token.
- Memoryd: async TokenAuthorizer — admin all verbs; pod Append/Load only AND
  owner==path id (else 404 no-oracle); user owner-match (else 404) with the
  create/bind path (Append may bind an owner-less record — rebuild/heal
  recovery). AuthzErr::NotFound added; SPEC-006 mapping 401/403/404 split in
  error_response. 6 call sites `.await`ed. Config REQUIRES VIHS_TOKEN_PEPPER
  (memoryd verifies tokens minted by another process — an ephemeral pepper
  is broken by design); main.rs wires the strict authorizer.
- Create-path owner binding is IDEMPOTENT: stamps when the record is missing
  OR has no owner (heal-only records from rebuild/recovery would otherwise be
  permanently inaccessible). Fixes the parallel-suite race where
  integ_rebuild's heal created owner-less records before integ_assign's
  create_session (root-caused via RUST_LOG=memoryd=debug authz tracing).
- VIHS_TOKEN_PEPPER generated (43-char base64url) in .env; ENVIRONMENT.md row
  widened to `all` with shared-across-services note; .env.example row.
- Tests: crates/memoryd/tests/authz.rs (10 — pod second-session 404, pod
  forbidden verb 403, signed-URL X-Amz-Expires=900, user foreign 404,
  missing/invalid 401, owner delete, ownerless bind); ensure_shared_pepper()
  helper in the 5 test files that hit the network memoryd (cargo-test does
  not source .env — without it build_state mints with an ephemeral pepper
  the strict memoryd rejects); integ_assign mints real tokens; pod
  MemoryClient fixture mints a real user token via POST /admin/tokens
  (spawning the orchestrator if needed).

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
- M4: the security-check.sh redaction gate was silently SKIPping — it runs
  `cargo test --workspace redaction` (FILTER BY TEST NAME), and M1–M3 added
  no test whose name contains "redaction". M4's tests are named
  `redaction_*` so the gate actually executes them now (7 pass).
- M4: `OrchError::retryable()` returned FALSE for `RateLimited` — a real
  SPEC-006 R1 violation that M3's authz work didn't touch (no RateLimited
  was ever produced). M4's rate limiter produces it, so the bug surfaced
  exactly when it mattered.
- M4: the LINT gate greps vihs-core/src for the literal sibling crate names
  (`memoryd`, `orchestrator`) — a redact.rs unit-test fixture string
  ("memoryd listening on…") tripped it. The Layer-0 crate must not even
  NAME its siblings in comments/tests.
- M4: the `.env` display redaction (`="` → `***`) hit a test-file write via
  write_file — the `format!("Authorization: Bearer {token}")` fixture line
  arrived corrupted. Worked around by building the "Bearer " prefix with
  `concat!()` in the test so no literal `Bearer "` sequence exists in the
  source file (also keeps the file clean for the secret-scan pattern).
- Pre-existing (M3, still open): dependency-audit.sh swallows cargo-audit's
  non-zero exit (`|| echo NOTE not installed`) — 3 rustls-webpki RUSTSECs
  via aws-smithy are printed but never fail the gate. aws-sdk upgrade
  deferred (dependency swap — needs its own decision). Not M4-introduced.

- M3: the strict owner check exposed a parallel-suite race — the memoryd
  `integ_rebuild` test replays the WHOLE shared bucket and `heal()`s every
  session, creating index records with NO owner field. A fixed-sid test
  (integ_assign's `session-fake-1`) then 404'd its own create_session because
  the record pre-existed owner-less. Root cause was found only via
  RUST_LOG=memoryd=debug authz tracing — and the first log read misled: the
  snap_owner=Some("") was `unwrap_or_default()` rendering a MISSING owner as
  an empty string, not an actual empty owner field.
- The pepper-sharing contract bites the TEST harness too: cargo-test does not
  source .env, so build_state falls back to an EPHEMERAL pepper while the
  network memoryd verifies with .env's — every cross-process token fails.
  Fix: `ensure_shared_pepper()` helper (reads .env, sets the var) in the 5
  test files that drive the network memoryd.
- The `cargo test -p memoryd authz` milestone command filters by test NAME
  under the rtk wrapper, not the module path — 0 matched until the tests were
  renamed with an `authz_` prefix.
- The assign frame's pod_token was a bare UUID the pod could never use; the
  pod agent already plumbed it into every memoryd call (agent.py:171) — so
  minting a REAL session-bound token fixed the whole data path at once.
- Pre-existing (logged, not M3-introduced): the `.env` display in tool output
  redacts `="` sequences as `***` — DISPLAY ONLY; actual bytes are correct
  (verified via ord() inspection) — don't "fix" files that look mangled.

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
- M4: rate limiting is in-memory (fixed-window per token_id), not Redis.
  Evidence: the orchestrator is a single process (dev + EP-009 target), and
  SPEC-005 A5 names per-token limits without a shared-storage requirement.
  Redis would add a round-trip per request for no correctness gain at this
  scale; the limiter is clock-injected so a distributed backend can replace
  it without touching handlers.
- M4: rate-limit key = stable token_id (16-byte base64url prefix) via the
  new `vihs_auth::token_id()`, NOT the raw token. Evidence: OBSERVABILITY.md
  forbids logging/storing tokens; the limiter must not hold secrets in
  memory. token_id is already the Redis lookup key, so it is stable and
  unique per token.
- M4: redaction middleware is a MakeWriter at the log boundary (ScrubWriter),
  plus owner_hash at emission sites. Evidence: a writer-level scrub catches
  ANY future leak (tokens, signed URLs) regardless of which code site emits
  it; owner ids can't be pattern-matched generically (session ids are also
  UUIDs), so raw owner ids are prevented at the source instead. Both layers
  together satisfy OBSERVABILITY.md's tested redaction.
- M4: audit events are `session_deleted` (orchestrator route + memoryd
  durable path) and `token_minted` (admin mint). SPEC-005 A7 requires hard
  delete to be audited ids-only; mint is a security-relevant admin action
  whose raw token must never appear in logs. Session create/append are not
  audited — volume would bury the security signal (rate limiter + authz
  already cover them).
- M4 files beyond §6 (AGENTS §5): vihs-core/src/redact.rs and
  vihs-auth token_id() are the shared helpers both services' middleware
  depend on — the layer-0 home is required by ARCHITECTURE §3 (no
  cross-service import). memoryd/src/api.rs + authz.rs changes are the
  owner-id redaction fixes and the durable delete audit. All M4-necessary.
- M3: shared token store moved to NEW crate `vihs-auth` (Layer 0) instead of
  duplicating the crypto in memoryd. Evidence: SPEC-005 A1 (memoryd verifies
  tokens minted by orchestrator); vihs-core declares itself pure zero-I/O
  (lib.rs + ARCHITECTURE.md §3), so auth gets its own crate. Two independent
  implementations of the same pepper+argon2 logic is exactly the drift the
  milestone exists to prevent. ARCHITECTURE.md updated (L2 spec-update with
  this evidence).
- M3: memoryd REQUIRES VIHS_TOKEN_PEPPER (Config::from_env `get()`, len≥16
  assert). An ephemeral per-process pepper is broken BY DESIGN for memoryd —
  it verifies tokens minted by another process. Orchestrator keeps the dev
  ephemeral fallback (EP-006 §9) but .env now sets the shared value so both
  match in every environment.
- M3: create-path owner binding is idempotent AND a valid user token may bind
  an owner-less record on Append (Load/Delete still 404). Heal-only records
  from rebuild/recovery have no owner; without the bind they are permanently
  inaccessible. Risk accepted: the first valid user token to append claims an
  orphaned session — session ids are 128-bit random and only the orchestrator
  mints tokens, so there is no practical hijack vector. Foreign-owner probes
  on OWNED sessions still 404 (A4 no-oracle).
- M3: per-assignment pod tokens are REAL store mints (SPEC-005 A3) — owner =
  session id, scope Pod, 15-min TTL, minted by `router::assign`, carried in
  the assign frame. The bare-UUID `pod_token()` helper is deleted; the pod
  agent's existing frame-token plumbing (agent.py:171) now verifies.
- M3: the pod MemoryClient pytest fixture mints a REAL user token via
  POST /admin/tokens (spawning the orchestrator if not running) because the
  strict memoryd rejects fake tokens. Pod-token semantics (session binding,
  verbs) are covered by the Rust authz suite — the pytest tests exercise the
  HTTP contract with a verifiable bearer.
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

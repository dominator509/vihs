# SPEC-005 — Auth, Ownership, Permissions

Status: accepted · Owner: djw · Roadmap phase: 5 · Linked ExecPlans: EP-004, EP-006

## User-visible goal
Only you can touch your sessions. Knowing a session's ID gives an attacker
nothing.

## Non-goals
OIDC/SSO (v2, ADR-006); self-serve signup; roles beyond the three scopes.

## Terms / model
Principals: owner (user token), pod (pod token), admin (admin token).
Tokens are opaque 32-byte random strings, base64url. Server stores
`token_id → {owner_id, scope, argon2id(token+pepper), expiry, revoked}`.
Lookup by token_id prefix, constant-time verify.

## Required behavior
- A1 Every session-scoped route: resolve bearer → principal; owner routes
  require `session.owner == principal.owner_id`; pod routes require the pod
  token's bound `session_id` == path id AND allowed verbs only (append event,
  load memory URL, PUT audio). Anything else 404/403 per convention.
- A2 Resume is ownership-gated: `session_id` is necessary but never
  sufficient (INV/§6.5). Negative tests are mandatory.
- A3 Pod tokens: minted per assignment, TTL ≤15 min, renewed by orchestrator
  while the assignment holds, revoked on reassignment/drain. Pods never hold
  store credentials; they get signed URLs (GET memory, PUT audio) with the
  same TTL.
- A4 Foreign-owner probes return 404 (no ID oracle) and increment a metric.
- A5 Rate limits per token: session create ≤10/min, resume ≤30/min,
  signaling messages ≤50/s; over-limit → 429 retryable.
- A6 Admin scope only on the admin listener; user tokens are rejected there.
- A7 Hard delete requires owner (or admin) and is audited (ids only).
- A8 Token revocation is immediate (revoked flag checked on every resolve).

## Inputs/outputs/errors
Standard SPEC-006 envelope; 401 unauthenticated, 404 unknown-or-foreign
session, 403 scope violation on non-session admin routes, 429 rate.

## Data rules
Token table in Redis (`token:{token_id}` HASH) — registry addition to
SPEC-002 keys; pepper from env, never stored.

## Security rules
Constant-time compares; argon2id parameters documented in code constants;
TLS assumed at proxy; no tokens in logs/URLs (WS uses header or first-message
auth frame, never query string).

## Observability
`vihs_authz_denied_total{kind}`, resume-denied alert (OBSERVABILITY).

## Required tests
Owner-match matrix over every session-scoped route (owner ok / foreign 404 /
no auth 401); pod-token boundary (second session 404; forbidden verb 403);
expiry + revocation tests; signed-URL expiry test; rate-limit tests.

## Acceptance criteria
security-check.sh green; the authz matrix test enumerates routes FROM the
SPEC-003 registry so new routes fail closed if untested.

# SPEC-006 — Error Taxonomy, Retries, Failure States

Status: accepted · Owner: djw · Roadmap phases: 3–6 · Linked ExecPlans: EP-004, EP-005, EP-007

## User-visible goal
Failures degrade gracefully: the user sees plain language and a path forward;
the conversation survives everything short of intentional deletion.

## Non-goals
Automatic cross-region failover; client-side offline mode.

## Error envelope (all HTTP/WS surfaces)
`{"error":{"code":"snake_case","message":"human text","retryable":bool}}`
Codes are a registry (add here first): `unauthenticated`, `not_found`,
`scope_violation`, `rate_limited`, `no_capacity`, `integrity_hold`,
`invalid_event`, `store_unavailable`, `pod_unavailable`, `bad_signal`,
`internal`.

## Retry classes
- R0 never retry (4xx logic errors: invalid_event, unauthenticated…).
- R1 caller-retry with backoff (store_unavailable, rate_limited, 503s):
  jittered exponential, base 250 ms, cap 5 s, max 6 tries unless stated.
- R2 infrastructure-retry (pod append buffer → memoryd: unbounded retries,
  bounded QUEUE of 64, health-degrade on full — ARCHITECTURE §9).

## Failure states by subsystem (required behavior)
- Pipeline stage crash mid-turn: abort the response cleanly (AbortBus), emit
  `kind:note` event `stage_error` (no user text), speak the fixed recovery
  utterance, return to listening. Two stage crashes in one session → pod
  marks itself degraded; orchestrator drains it; user resumes elsewhere.
- Pod death: orchestrator detects stale ping (>15 s) → terminate/replace →
  affected users see F4 reconnect → resume path; at most the in-flight turn
  is lost (INV-3; the recovered transcript never claims unheard speech,
  INV-1).
- memoryd down: pods buffer (R2); orchestrator serves cached session lists
  degraded; resume returns 503 retryable.
- Redis down: memoryd/orchestrator fail readyz; on recovery or replacement,
  `--rebuild-index` path documented (OPERATIONS).
- Chain verification failure on load: `integrity_hold` (409), resume denied,
  SEV1 — never auto-"repair".
- Signaling abuse (schema/size): `bad_signal`, close WS after 3 strikes.

## Logging behavior
Every error logged once at the boundary that handles it, with `err` code and
correlation ids, redaction rules applied; retries log at debug, not info
(no log storms).

## User-facing messages (client copy registry)
no_capacity→"All avatars are busy — you're in line."; integrity_hold→"This
session is locked for review."; pod_unavailable/reconnect→"Reconnecting…";
generic internal→"Something went wrong on our side." Raw codes never shown.

## Required tests
Envelope schema test on every route (contract suite); retry-class unit tests
(backoff math, R2 bounded queue + degrade); each failure state above has a
failure-mode test in EP-007 (fault injection: kill pod, pause memoryd,
corrupt a log copy for the fsck path).

## Acceptance criteria
Chaos suite demonstrates each subsystem row's required behavior; no
unclassified error codes appear in logs during the suite.

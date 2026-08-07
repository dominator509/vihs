# SECURITY.md — VIHS

## Goals
Transcripts are sensitive personal data. Protect: session content
(confidentiality, integrity), resume path (only the owner resumes), the
control plane (pods are semi-trusted, disposable), and stored data at rest.

## Threat model summary
- T1 Leaked/brute-forced `session_id` → attacker replays transcript.
  Control: ownership-gated resume; ID alone authorizes nothing (SPEC-005).
- T2 Compromised/stale pod → standing credentials abused.
  Control: pods get only short-lived signed URLs + per-assignment pod tokens
  scoped to one session; revoked on reassignment; no store creds on pods.
- T3 Tampered history → altered "what was said".
  Control: blake3 hash chain; `chain-fsck` in verify + on resume load.
- T4 Storage compromise at rest. Control: SSE on object store + FDE (ADR-008);
  envelope-crypto upgrade path documented.
- T5 Signaling/API abuse (schema bombs, oversized frames, replay).
  Control: strict schema validation, size caps, per-token rate limits.
- T6 Log exfiltration of content. Control: redaction rules (transcript text,
  tokens, owner ids never at info level; OBSERVABILITY.md).

## Authentication
Opaque bearer tokens (ADR-006), argon2id-hashed at rest, constant-time
compare on lookup key, TLS everywhere external. Token scopes: `user` (own
sessions), `pod` (single assigned session, append+fetch only), `admin`
(operator API). Expiry: user tokens operator-configured; pod tokens ≤ 15 min,
renewed by orchestrator while assignment holds.

## Authorization rules
Every session-scoped route resolves `token → owner` then checks
`session.owner == owner` (or pod token's bound session). 404 (not 403) for
sessions the caller cannot see, to avoid ID oracle. Admin routes separate
listener/port.

## Input validation / output encoding
All external inputs validated at the edge per SPEC-006: JSON schema, max
sizes (signaling msg ≤ 16 KiB; append event ≤ 64 KiB; audio_ref must match
the session's own prefix). Client renders transcript markdown through a
sanitizer; no raw HTML from model output.

## Secret management
Secrets only via environment (ENVIRONMENT.md registry) or mounted files;
never in code, logs, or the repo. `security-check.sh` runs a secret scan
(gitleaks-style patterns) over the tree and fails on hits. `.env` gitignored.

## Dependency security
`cargo audit` + `pip-audit` in `dependency-audit.sh`; criticals block release
(RELEASE.md). Pinned lockfiles committed.

## Logging redaction
Forbidden in logs at any level shipped off-host: event `text`, raw audio,
bearer tokens, signed URLs, owner ids (hash if needed for correlation).
Redaction has unit tests (TESTING.md).

## Data protection / retention / deletion
SSE at rest; TLS in transit; TTL `SESSION_TTL_DAYS` (default 90) enforced by
memoryd sweep; hard delete removes event log, transcript.md, memory.md, audio
objects, and the Redis index in that order, idempotently — a true
right-to-be-forgotten, not a soft hide (SPEC-002 D-9). Deletion emits an
audit log line (ids only).

## Safe migration rules
Event schema evolves by `v` bump + tolerant readers; historical events are
never rewritten (would break the chain — that's the point).

## API security
Rate limits per token on session create/resume; CORS locked to the operator
origin; WebSocket signaling requires the same bearer as HTTP; no cookies, so
CSRF surface is nil (documented; revisit if cookie auth ever added).

## Security checklist (run at EP-006 exit and each release)
[ ] security-check.sh green  [ ] authz tests green (foreign owner 403/404)
[ ] signed URL expiry test green  [ ] redaction tests green
[ ] dependency audit reviewed  [ ] no secrets in tree  [ ] TLS termination
documented for deploy target  [ ] hard-delete drill leaves zero objects

## Security STOP conditions
Any change weakening ownership gating, widening pod credentials, disabling
chain verification, or logging redacted classes ⇒ STOP S3 (AGENTS.md).

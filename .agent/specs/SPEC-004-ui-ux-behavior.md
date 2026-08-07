# SPEC-004 — Client UI/UX Behavior

Status: accepted · Owner: djw · Roadmap phase: 4 · Linked ExecPlans: EP-005

## User-visible goal
A single-page client where connecting, talking, interrupting, resuming,
exporting, and deleting all feel obvious — and cold starts feel intentional,
not broken.

## Non-goals
Framework/bundler; theming; mobile-native; multi-avatar picker beyond a
persona dropdown; account management UI (tokens are operator-issued).

## Screens & user flows
One page, four regions: header (session picker + "Resume last session"),
stage (avatar video), status strip, controls (mic toggle, captions toggle,
end, export, delete-with-confirm).
- F1 First visit: token prompt (stored in memory only, not localStorage by
  default; "remember on this device" opt-in) → persona select → Connect.
- F2 Conversation: mic level indicator; live captions under the stage when
  toggled; avatar video plays; user just talks (barge-in needs no button).
- F3 Cold start / queued: on signaling `state: cold_start|queued`, play the
  pre-loaded idle eyecandy loop with "Warming up your avatar…" — masks the
  15–30 s spin-up; never a spinner over black.
- F4 Drop/reconnect: on media loss, status "Reconnecting…", auto retry within
  connection-token life; on failure, offer Resume (full resume path).
- F5 Resume: list sessions (GET /v1/sessions), pick one → resume → avatar's
  first utterance continues context; captions show last 2 committed turns
  greyed for orientation.
- F6 Export: downloads transcript.md. F7 Delete: typed-confirm modal ("this
  cannot be undone"), then 204 → session leaves the list.

## States (each region defines loading/empty/error)
Stage: idle-loop | live | reconnecting | error(with retry). Session list:
loading | empty ("No sessions yet") | error. Every error state shows the
SPEC-006 user message, never a raw code.

## Inputs / outputs
Mic (getUserMedia, echoCancellation on — the avatar's own audio must not
trigger fake barge-ins client-side; server VAD still guards), signaling WS,
media track, caption data channel.

## Error states
Token invalid → inline auth error. `no_capacity` queued → F3 with eta hint.
`integrity_hold` → "This session is locked for review" + support pointer.

## Accessibility rules
All controls keyboard-operable and labeled; captions toggle is the a11y
path for avatar speech; status conveyed by text+icon, not color alone;
focus not stolen on state changes; prefers-reduced-motion honored by the
idle loop (static poster fallback).

## Security rules
Bearer only in memory unless opted in; transcript markdown sanitized before
render; media/TLS per deployment docs.

## Performance rules
Client adds no perceptible latency: audio track attached before answer
completes rendering; caption deltas applied incrementally.

## Observability
Client emits (to orchestrator, batched) only: connect result, reconnect
count, cold-start-mask duration. No content telemetry.

## Required tests
E2E (mock stages) drives F1–F5 headless; caption-render sanitizer unit test;
keyboard-only walkthrough scripted in E2E.

## Acceptance criteria
All flows pass E2E; accessibility checklist rows in PRODUCTION_READINESS
green; cold-start mask verified in staging (real spin-up).

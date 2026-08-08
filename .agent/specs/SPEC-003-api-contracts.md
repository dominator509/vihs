# SPEC-003 — API Contracts (orchestrator, memoryd, signaling, pod)

Status: accepted · Owner: djw · Roadmap phases: 2, 3 · Linked ExecPlans: EP-003, EP-004
This file is the ROUTE REGISTRY. Agents must not invent routes (AGENTS §6).

## User-visible goal
One small, predictable HTTP+WebSocket surface: create/resume/export/delete a
session and get connected to a pod. Everything else is internal.

## Non-goals
Public third-party API; GraphQL; API versioning beyond `/v1` prefix in v1.

## Required behavior
The route tables below ARE the required behavior; conventions bind all rows.

## Conventions
JSON bodies; `Authorization: Bearer <token>`; errors use the SPEC-006
envelope `{"error": {"code": "...", "message": "...", "retryable": bool}}`;
unknown sessions return 404 (never 403 — no ID oracle, SPEC-005); all ids are
UUIDv4 strings; timestamps RFC3339 UTC.

## Orchestrator public API (`VIHS_ORCH_ADDR`, user tokens)
| Method+Path | Request | Response 2xx | Errors |
|---|---|---|---|
| POST /v1/sessions | `{persona_id}` | 201 `{session_id, created_at}` | 401, 429 |
| POST /v1/sessions/{id}/resume | `{}` | 200 `{connect: {ws_url, connection_id}, last_turn_id}` | 401, 404, 409 `integrity_hold`, 503 `no_capacity` (queued: body has `queued: true, eta_hint_s`) |
| POST /v1/sessions/{id}/connect | `{}` (fresh session first connect) | 200 same as resume | same |
| GET /v1/sessions | — | 200 `{sessions: [{session_id, updated_at, turns}]}` (owner's, newest first) | 401 |
| GET /v1/sessions/{id}/transcript | — | 200 `text/markdown` (streamed from memoryd render) | 401, 404 |
| DELETE /v1/sessions/{id} | — | 204 (hard delete, D-9; idempotent) | 401, 404 |
| GET /healthz, /readyz | — | 200 | 503 |
Static client served at `/` (embedded).

## Signaling WebSocket `wss://…/v1/signal/{connection_id}` (same bearer)
Messages (client↔orchestrator↔pod relay; ≤16 KiB each; strict schema):
```json
c→s {"t":"auth","token":"..."}        // FIRST message only, browser path (SPEC-005:
                                       // "header or first-message auth frame, never
                                       // query string"). Consumed by the orchestrator,
                                       // never forwarded to the pod. Headered clients
                                       // (pod/harness) skip it.
c→s {"t":"offer","sdp":"..."}          s→c {"t":"answer","sdp":"..."}
c↔s {"t":"ice","candidate":{...}}
s→c {"t":"state","v":"queued|assigning|cold_start|connected|reconnect"}
s→c {"t":"turn","cfg":{"turn_url":"...","user":"...","pass":"..."}}   // only if TURN configured
s→c {"t":"caption","turn_id":42,"delta":"...","final":false}          // live captions (relay from pod data or ws)
s→c {"t":"error","code":"...","message":"..."}
```
`cold_start` instructs the client to run the idle eyecandy loop. Reconnect
of the media path without a new resume call is allowed while the connection
token is live (≤15 min).

## Orchestrator admin API (`VIHS_ADMIN_ADDR`, admin tokens, local only)
| Route | Purpose |
|---|---|
| GET /admin/pods | pod registry: id, addr, state, fill/cap, last_ping age |
| POST /admin/pods/{id}/drain | stop new assignments; sessions finish/resume elsewhere |
| GET /admin/scale | autoscaler state: fill, queue depth, warm floor, last decisions |
| POST /admin/tokens | mint user/admin tokens `{owner_id, scope}` → `{token}` (shown once) |

## MCP server (ADR-011) — same ops, MCP transport
The MCP server (`mcpd`, listens on `VIHS_MCP_ADDR`) is a thin adapter over the
orchestrator operations above: one implementation, two transports. It speaks
JSON-RPC 2.0 (MCP 2025-03-26): `initialize`, `tools/list`, `tools/call`.
Every tool below has an EXACT HTTP twin in the registries above; schema
mirrors the route body; auth uses the same bearer tokens. Tool names are
`vihs_*` prefixed so multi-server MCP hosts (AXIOM) keep namespaces disjoint.

| Tool | Arguments → Result | Twin route |
|---|---|---|
| vihs_session_create | `{persona_id}` → `{session_id, created_at}` | POST /v1/sessions |
| vihs_session_resume | `{session_id}` → `{ws_url, connection_id, last_turn_id}` | POST /v1/sessions/{id}/resume |
| vihs_session_list | `{}` → `{sessions: [{session_id, updated_at, turns}]}` | GET /v1/sessions |
| vihs_session_transcript | `{session_id}` → `{transcript_md}` | GET /v1/sessions/{id}/transcript |
| vihs_session_delete | `{session_id}` → `{deleted: true}` | DELETE /v1/sessions/{id} |
| vihs_pods_list | `{}` → `{pods: [{id, addr, state, fill, cap, last_ping_age_s}]}` | GET /admin/pods |
| vihs_pod_drain | `{pod_id}` → `{draining: true}` | POST /admin/pods/{id}/drain |
| vihs_scale_status | `{}` → `{fill, queue_depth, warm_floor, last_decisions}` | GET /admin/scale |
| vihs_token_mint | `{owner_id, scope}` → `{token}` (shown once) | POST /admin/tokens |

Contract tests: for each tool, a JSON-RPC `tools/call` fixture asserting the
same schema the route contract test asserts (added in EP-004, same milestone
as the route). Errors map to MCP `isError: true` with SPEC-006 codes.

## Orchestrator internal API for pods (pod tokens)
| Route | Purpose |
|---|---|
| POST /internal/pods/register | pod boot: `{pod_id, addr, cap, versions}` → assignment channel setup |
| POST /internal/pods/{id}/health | 5 s ping: `{fill, stages_ready, gpu_util, buffer_depth}` |
| WS /internal/pods/{id}/assign | orchestrator→pod assignments: `{"t":"assign","session_id","connection_id","memory_url","audio_put_url","pod_token","resume":bool,"cursor":{...}}` and `{"t":"revoke",...}` |

## memoryd API (`VIHS_MEMORYD_ADDR`, internal; orchestrator + pod tokens)
| Method+Path | Request | Response | Notes |
|---|---|---|---|
| POST /v1/sessions/{id}/events | event body minus prev_hash/hash | 200 `{status:"committed"|"duplicate","hash","turn_id"}` / 400 Rejected / 503 Retryable | pod token bound to {id}; writer fills chain fields (SPEC-002 D-5) |
| GET /v1/sessions/{id}/memory | — | 200 `{memory_url_signed}` or bytes for orchestrator | epoch-stable within epoch |
| GET /v1/sessions/{id}/transcript | — | 200 markdown | render-on-demand (INV-5) |
| POST /v1/sessions/{id}/load | `{}` | 200 `{cursor:{tip_hash,last_turn_id,epoch}, memory_url}` | runs fsck-tail; 409 integrity_hold on chain failure |
| DELETE /v1/sessions/{id} | — | 204 | D-9 sequence |
| POST /v1/sessions/{id}/compact | `{}` | 202 | orchestrator-triggered safety valve; normally self-triggered |
| GET /healthz, /readyz | — | 200 | |

## Pod local surface
`GET /health` `{stages:{vad,stt,llm,tts,lipsync,mux}: ready|loading|error,
fill, cap}`. Media: WebRTC (SDP via signaling). Captions: WebRTC data
channel `captions` mirroring the `caption` message shape.

## Contract test rules
Every row above has: (a) a Rust producer test asserting response schema, and
(b) a consumer fixture test (Python for pod-facing, JS fixture check for
client-facing) — added in the same milestone as the route (TESTING.md).

## Error states / security / observability
SPEC-006 taxonomy; SPEC-005 authz on every session-scoped row; per-route
latency + status-class counters (SPEC-007).

## Acceptance criteria
Route-registry check in verify.sh finds no route in code absent from this
table; all contract tests green; 404-not-403 behavior verified for foreign
owners on every session-scoped route.

# EP-004 — API / Service Layer (orchestrator)

## 1. Purpose / Big Picture
Build the control plane's front door: session API, WebSocket signaling relay,
pod registry + router, and the autoscaler engine (warm pool, preemptive
scale, cooldown drain, crash replacement) — against a MOCK pod provider so
every behavior is CI-testable without a GPU or cloud account.

## 2. Scope
`crates/orchestrator`: public API rows (SPEC-003), admin API, internal pod
API + assignment WS, signaling relay, router, autoscaler, `PodProvider`
trait + `mock` driver, queueing, static client serving. Contract +
simulation tests. **`crates/mcpd`: MCP server (ADR-011) exposing the same
orchestrator ops as `vihs_*` tools (SPEC-003 §MCP), with JSON-RPC contract
fixtures mirroring the route contract tests.**

## 3. Non-goals
Real RunPod driver (EP-009). Real auth tokens (Authorizer trait, permissive
dev impl; EP-006 replaces). No media handling — SDP/ICE bytes are relayed
opaquely. No TURN deployment (config plumb-through only).

## 4. Context and Orientation
SPEC-003 is the route registry; ARCHITECTURE §8 (flows) and §13 (scaling
rules) are normative. The autoscaler is deliberately a pure decision function
+ an executor so simulations test the policy without time or cloud.

## 5. Files to Read First
SPEC-003, SPEC-005 (A-rules to leave seams for), SPEC-006 (envelope, retry
classes), ARCHITECTURE §8/§13, ENVIRONMENT.md (POD_MAX_SESSIONS,
SCALE_UP_FILL, WARM_POOL_FLOOR, POD_COOLDOWN_SECS, PROVIDER).

## 6. Files to Change
crates/orchestrator/src/{main.rs,api_public.rs,api_admin.rs,api_internal.rs,
signal.rs,router.rs,scaler.rs,provider.rs,provider_mock.rs,registry.rs,
queue.rs,authz.rs,error.rs,client_static.rs},
crates/orchestrator/tests/{contract_public.rs,sim_scaler.rs,integ_assign.rs,
integ_resume_flow.rs}, client/ placeholder untouched (EP-005 owns it).

## 7. Interfaces and Contracts
```rust
// provider.rs — every cloud is a driver behind this seam (ADR-008/§9 menu)
#[async_trait] pub trait PodProvider: Send + Sync {
    async fn deploy(&self, spec: &PodSpec) -> Result<PodId, ProvErr>;   // returns immediately; pod registers itself when up
    async fn terminate(&self, id: &PodId) -> Result<(), ProvErr>;
    fn cold_start_hint(&self) -> Duration;                              // for queue eta_hint_s
}

// registry.rs — Redis-backed pod registry (keys per SPEC-002 registry)
pub struct PodState { pub id: PodId, pub addr: SocketAddr, pub cap: u32,
    pub fill: u32, pub last_ping: Instant, pub state: PodPhase } // Booting|Ready|Draining|Dead

// scaler.rs — HARD PIECE: pure policy + executor split
pub struct FleetView { pub pods: Vec<PodState>, pub queue_len: usize,
    pub warm_floor: u32, pub scale_up_fill: f32, pub cooldown: Duration,
    pub now: Instant }
pub enum ScaleAction { Deploy { count: u32 }, StartCooldown(PodId),
    CancelCooldown(PodId), Terminate(PodId), Replace(PodId), None }

/// Decide is PURE: no clock reads, no I/O — everything arrives in FleetView.
/// Rules (ARCHITECTURE §13):
///  1. Dead pods (stale ping) => Replace (terminate+deploy) — resume path
///     reconnects their users; count them out of capacity immediately.
///  2. Preemptive scale-up: if ready capacity_used >= SCALE_UP_FILL of ready
///     capacity AND no pod is Booting, Deploy{1}. Never wait for a queued
///     user to exist — the 4/5 rule fires on fill, the queue rule below is
///     the backstop.
///  3. Backstop: queue_len > 0 and no Booting pod => Deploy{ceil(queue/cap)}.
///  4. Scale-down: a Ready pod with fill==0 starts cooldown; any assignment
///     cancels it; cooldown expiry => Terminate — UNLESS terminating would
///     drop ready+booting pods below warm_floor.
///  5. warm_floor also bootstraps: fewer total live pods than floor => Deploy.
pub fn decide(v: &FleetView) -> Vec<ScaleAction> { /* implement exactly */ }
```
Simulation tests (`sim_scaler.rs`) drive `decide` through scripted timelines:
cold morning (floor bootstrap), rush (fill climbs 0→cap across pods, asserts
deploy fires AT 4/5 not later, and only one Booting at a time), crash
(Replace + capacity math), lull (cooldown → terminate, floor respected),
flap-guard (assign during cooldown cancels it). No sleeps — `now` is data.

```rust
// router.rs — assignment
/// pick(): least-loaded Ready pod with fill < cap (hard cap enforcement is
/// HERE — exceeding it spikes VRAM and collapses latency for everyone on the
/// pod). Ties: lowest pod_id for determinism. None => enqueue + return
/// no_capacity{queued:true, eta_hint}.
/// On assignment: mint pod_token + signed memory URL via memoryd load(),
/// send `assign` frame on the pod's internal WS, bump fill, cancel cooldown.
```

Signaling relay (`signal.rs`): client WS ↔ pod, opaque SDP/ICE relay, state
frames (`queued|assigning|cold_start|connected|reconnect`), strict schema +
16 KiB cap + 3-strike close (SPEC-006). Resume flow per ARCHITECTURE §8:
authorize owner → memoryd load → assign with cursor + memory URL.

## 8. Milestones
M1 Registry + internal pod API (register, health, assign WS) with a FAKE pod
   fixture (tiny Rust test client). Validation: `--test integ_assign`.
   Expected: register→ready→assign frame received→fill bumped.
M2 Scaler policy + simulations. Validation: `--test sim_scaler`. Expected:
   all five scripted timelines pass; property check: capacity_used never
   exceeds sum(cap) and floor never violated across random timelines.
M3 Public API + queueing + signaling relay. Validation: `--test
   contract_public` + `--test integ_resume_flow` (against live memoryd from
   EP-003, dev services). Expected: full fresh-session and resume flows green
   with the fake pod echoing SDP.
M4 Admin API + drain + static client serving. Validation: contract tests for
   admin rows; drain test: draining pod gets no new assignments, cooldown
   semantics per policy.
M5 Wire into scripts/CI. Validation: `sh scripts/test-integration.sh`
   INTEGRATION OK including orchestrator suites.

## 9. Concrete Steps
Milestone order. Route handlers stay thin: parse/validate → typed call →
envelope. Every route added lands with its SPEC-003 contract test (route-
registry gate).

## 10. Validation and Acceptance
INTEGRATION OK; acceptance: end-to-end resume flow test proves ARCHITECTURE
§8 sequence with real memoryd; cap enforcement test (cap=1, two connects ⇒
second queued); crash-replacement sim green.

## 11. Idempotence and Recovery
Sim tests are pure. Integration tests namespace sessions and fake pods.
Orchestrator state is Redis + memoryd — restart-safe by construction; the
integ suite restarts orchestrator mid-flow once to prove it.

## 12. Progress
- [ ] M1 registry+assign  - [ ] M2 scaler sims  - [ ] M3 public+resume
- [ ] M4 admin+drain+static  - [ ] M5 scripts/CI

## 13. Surprises & Discoveries
## 14. Decision Log
## 15. Outcomes & Retrospective

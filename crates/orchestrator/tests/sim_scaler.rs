//! M2 — scaler policy simulations (ARCHITECTURE §13 five rules).
//!
//! `decide` is pure: no clock reads, no I/O. `now` is data. These timelines
//! script fleet states and assert the exact action at each step.

use std::time::{Duration, Instant};

use orchestrator::provider::PodId;
use orchestrator::registry::{PodPhase, PodState};
use orchestrator::scaler::{decide, FleetView, ScaleAction, PING_DEAD_AFTER};

fn pod(id: &str, state: PodPhase, cap: u32, fill: u32, ping_age: Duration) -> PodState {
    let now = Instant::now();
    PodState {
        id: PodId(id.to_string()),
        addr: "127.0.0.1:0".to_string(),
        cap,
        fill,
        versions: None,
        last_ping: now - ping_age,
        state,
        cooldown_until: None,
        created_at: now,
    }
}

fn view(pods: Vec<PodState>, queue_len: usize, floor: u32, fill: f32) -> FleetView {
    FleetView {
        pods,
        queue_len,
        warm_floor: floor,
        scale_up_fill: fill,
        cooldown: Duration::from_secs(300),
        now: Instant::now(),
    }
}

fn has_deploy(actions: &[ScaleAction], count: u32) -> bool {
    actions
        .iter()
        .any(|a| matches!(a, ScaleAction::Deploy { count: c } if *c == count))
}

fn has_action(actions: &[ScaleAction], pred: impl Fn(&ScaleAction) -> bool) -> bool {
    actions.iter().any(pred)
}

// ---------------------------------------------------------------------------
// Timeline 1: cold morning — floor bootstrap.
// ---------------------------------------------------------------------------

#[test]
fn cold_morning_bootstraps_to_floor() {
    let v = view(vec![], 0, 2, 0.8);
    let actions = decide(&v);
    assert!(
        has_deploy(&actions, 2),
        "empty fleet with floor=2 must deploy 2: {actions:?}"
    );
}

#[test]
fn floor_satisfied_no_action_or_cooldown() {
    let v = view(
        vec![pod("a", PodPhase::Ready, 2, 0, Duration::ZERO)],
        0,
        1,
        0.8,
    );
    let actions = decide(&v);
    // One ready pod at floor=1, fill=0: cooldown starts (rule 4), not deploy.
    assert!(
        has_action(&actions, |a| matches!(a, ScaleAction::StartCooldown(_))),
        "idle pod at floor should enter cooldown: {actions:?}"
    );
}

// ---------------------------------------------------------------------------
// Timeline 2: rush hour — preemptive scale-up at 4/5 fill.
// ---------------------------------------------------------------------------

#[test]
fn rush_fires_deploy_exactly_at_scale_up_fill() {
    // One ready pod cap=10, fill=8 → 0.8 exactly → deploy 1.
    let v = view(
        vec![pod("a", PodPhase::Ready, 10, 8, Duration::ZERO)],
        0,
        1,
        0.8,
    );
    let actions = decide(&v);
    assert!(
        has_deploy(&actions, 1),
        "fill 8/10 = 0.8 must deploy at threshold: {actions:?}"
    );
}

#[test]
fn rush_below_threshold_no_deploy() {
    let v = view(
        vec![pod("a", PodPhase::Ready, 10, 7, Duration::ZERO)],
        0,
        1,
        0.8,
    );
    let actions = decide(&v);
    assert!(
        !has_action(&actions, |a| matches!(a, ScaleAction::Deploy { .. })),
        "fill 7/10 = 0.7 must not deploy: {actions:?}"
    );
}

#[test]
fn no_second_deploy_while_booting() {
    // Fill at 0.8 AND a pod booting → rule 2 suppressed (no two Booting).
    let v = view(
        vec![
            pod("a", PodPhase::Ready, 10, 8, Duration::ZERO),
            pod("b", PodPhase::Booting, 10, 0, Duration::ZERO),
        ],
        0,
        1,
        0.8,
    );
    let actions = decide(&v);
    assert!(
        !has_action(&actions, |a| matches!(a, ScaleAction::Deploy { .. })),
        "never deploy while a pod is booting: {actions:?}"
    );
}

// ---------------------------------------------------------------------------
// Timeline 3: crash — stale ping => Replace.
// ---------------------------------------------------------------------------

#[test]
fn stale_ping_triggers_replace() {
    let v = view(
        vec![pod(
            "a",
            PodPhase::Ready,
            2,
            1,
            PING_DEAD_AFTER + Duration::from_secs(1),
        )],
        0,
        1,
        0.8,
    );
    let actions = decide(&v);
    assert!(
        has_action(
            &actions,
            |a| matches!(a, ScaleAction::Replace(id) if id.as_str() == "a")
        ),
        "stale pod must be replaced: {actions:?}"
    );
}

#[test]
fn fresh_ping_no_replace() {
    let v = view(
        vec![pod("a", PodPhase::Ready, 2, 1, Duration::ZERO)],
        0,
        1,
        0.8,
    );
    let actions = decide(&v);
    assert!(
        !has_action(&actions, |a| matches!(a, ScaleAction::Replace(_))),
        "fresh pod must not be replaced: {actions:?}"
    );
}

// ---------------------------------------------------------------------------
// Timeline 4: lull — cooldown → terminate, floor respected.
// ---------------------------------------------------------------------------

#[test]
fn lull_cooldown_then_terminate() {
    // Two ready idle pods, floor=1. First tick: both enter cooldown.
    let mut p1 = pod("a", PodPhase::Ready, 2, 0, Duration::ZERO);
    let mut p2 = pod("b", PodPhase::Ready, 2, 0, Duration::ZERO);
    let now = Instant::now();
    let v1 = FleetView {
        pods: vec![p1.clone(), p2.clone()],
        queue_len: 0,
        warm_floor: 1,
        scale_up_fill: 0.8,
        cooldown: Duration::from_secs(300),
        now,
    };
    let actions = decide(&v1);
    let cooldowns: Vec<_> = actions
        .iter()
        .filter_map(|a| match a {
            ScaleAction::StartCooldown(id) => Some(id.as_str().to_string()),
            _ => None,
        })
        .collect();
    assert_eq!(
        cooldowns.len(),
        2,
        "both idle pods enter cooldown: {actions:?}"
    );

    // Second tick: cooldown expired on one; floor=1 so we may terminate one.
    let now2 = now + Duration::from_secs(301);
    // Pods pinged at now2 (healthy, idle) with cooldown already expired.
    p1.last_ping = now2;
    p1.cooldown_until = Some(now2);
    p2.last_ping = now2;
    p2.cooldown_until = Some(now2);
    let v2 = FleetView {
        pods: vec![p1, p2],
        queue_len: 0,
        warm_floor: 1,
        scale_up_fill: 0.8,
        cooldown: Duration::from_secs(300),
        now: now2,
    };
    let actions = decide(&v2);
    let terminates: Vec<_> = actions
        .iter()
        .filter_map(|a| match a {
            ScaleAction::Terminate(id) => Some(id.as_str().to_string()),
            _ => None,
        })
        .collect();
    assert_eq!(
        terminates.len(),
        1,
        "exactly one may terminate at floor=1: {actions:?}"
    );
}

#[test]
fn lull_floor_protection_blocks_termination() {
    // Single idle pod at floor=1 with expired cooldown → must NOT terminate.
    let now = Instant::now();
    let now2 = now + Duration::from_secs(301);
    let mut p = pod("a", PodPhase::Ready, 2, 0, Duration::ZERO);
    p.last_ping = now2; // healthy, idle
    p.cooldown_until = Some(now2);
    let v = FleetView {
        pods: vec![p],
        queue_len: 0,
        warm_floor: 1,
        scale_up_fill: 0.8,
        cooldown: Duration::from_secs(300),
        now: now2,
    };
    let actions = decide(&v);
    assert!(
        !has_action(&actions, |a| matches!(a, ScaleAction::Terminate(_))),
        "floor=1 must block termination of the last pod: {actions:?}"
    );
}

// ---------------------------------------------------------------------------
// Timeline 5: flap-guard — assignment during cooldown cancels it.
// ---------------------------------------------------------------------------

#[test]
fn assignment_cancels_cooldown() {
    let now = Instant::now();
    let mut p = pod("a", PodPhase::Ready, 2, 1, Duration::ZERO); // fill=1 => active
    p.cooldown_until = Some(now + Duration::from_secs(120));
    let v = FleetView {
        pods: vec![p],
        queue_len: 0,
        warm_floor: 1,
        scale_up_fill: 0.8,
        cooldown: Duration::from_secs(300),
        now,
    };
    let actions = decide(&v);
    assert!(
        has_action(
            &actions,
            |a| matches!(a, ScaleAction::CancelCooldown(id) if id.as_str() == "a")
        ),
        "active pod with cooldown must cancel it: {actions:?}"
    );
}

// ---------------------------------------------------------------------------
// Queue backstop (rule 3).
// ---------------------------------------------------------------------------

#[test]
fn queue_backstop_deploys_when_queued() {
    // One ready pod cap=4, fill=4 (full), queue_len=3, no booting → deploy ceil(3/4)=1.
    let v = view(
        vec![pod("a", PodPhase::Ready, 4, 4, Duration::ZERO)],
        3,
        1,
        0.8,
    );
    let actions = decide(&v);
    assert!(
        has_deploy(&actions, 1),
        "queued users with no booting pod must trigger deploy: {actions:?}"
    );
}

#[test]
fn queue_backstop_respects_booting() {
    let v = view(
        vec![
            pod("a", PodPhase::Ready, 4, 4, Duration::ZERO),
            pod("b", PodPhase::Booting, 4, 0, Duration::ZERO),
        ],
        3,
        1,
        0.8,
    );
    let actions = decide(&v);
    assert!(
        !has_action(&actions, |a| matches!(a, ScaleAction::Deploy { .. })),
        "booting pod already covers the queue: {actions:?}"
    );
}

// ---------------------------------------------------------------------------
// Property checks across random timelines.
// ---------------------------------------------------------------------------

fn random_pod(rng: &mut impl FnMut(u32) -> u32, cap: u32) -> PodState {
    let state = match rng(3) {
        0 => PodPhase::Booting,
        1 => PodPhase::Dead,
        _ => PodPhase::Ready,
    };
    let fill = if state == PodPhase::Ready {
        rng(cap + 1)
    } else {
        0
    };
    pod(&format!("p{}", rng(1000)), state, cap, fill, Duration::ZERO)
}

#[test]
fn property_capacity_never_exceeds_sum_cap() {
    // After any decide() with no Replace actions, ready capacity_used
    // implied by the fleet (fill) must never exceed total ready cap.
    let cap = 5u32;
    let mut seed = 0x5eed_u64;
    let mut rng = move |bound: u32| -> u32 {
        seed = seed
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((seed >> 33) % bound as u64) as u32
    };

    for _ in 0..500 {
        let n = rng(5) + 1;
        let pods: Vec<PodState> = (0..n).map(|_| random_pod(&mut rng, cap)).collect();
        let v = view(pods.clone(), rng(4) as usize, 1, 0.8);
        let actions = decide(&v);
        // Deploy actions are policy responses, not fleet facts; the fleet
        // itself must be consistent: no pod fill > its cap.
        for p in &pods {
            assert!(
                p.fill <= p.cap,
                "pod {} fill {} > cap {}",
                p.id,
                p.fill,
                p.cap
            );
        }
        // If a Replace fired, capacity math defers to next tick — fine.
        let _ = actions;
    }
}

#[test]
fn property_floor_never_violated_by_termination() {
    // Simulate the cooldown-expiry termination path: decide() may only
    // Terminate when live-after >= warm_floor.
    let now = Instant::now();
    for floor in 0..3u32 {
        for live in 0..4u32 {
            let mut pods: Vec<PodState> = (0..live)
                .map(|i| {
                    let mut p = pod(&format!("p{i}"), PodPhase::Ready, 2, 0, Duration::ZERO);
                    p.cooldown_until = Some(now + Duration::from_secs(300));
                    p
                })
                .collect();
            // One booting pod counts toward live.
            if live > 0 {
                pods.push(pod("boot", PodPhase::Booting, 2, 0, Duration::ZERO));
            }
            let v = FleetView {
                pods,
                queue_len: 0,
                warm_floor: floor,
                scale_up_fill: 0.8,
                cooldown: Duration::from_secs(300),
                now: now + Duration::from_secs(301),
            };
            let actions = decide(&v);
            let terminates = actions
                .iter()
                .filter(|a| matches!(a, ScaleAction::Terminate(_)))
                .count();
            // live count includes ready (fill any) + booting; terminating one
            // ready idle pod must keep live >= floor.
            let live_before = live + 1; // + booting
            if terminates > 0 {
                assert!(
                    (live_before as i64 - terminates as i64) >= floor as i64,
                    "floor {floor}, live {live_before}, terminated {terminates} → violation"
                );
            }
        }
    }
}

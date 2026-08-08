//! Autoscaler: PURE policy (`decide`) + executor. `decide` never reads the
//! clock or does I/O — everything arrives in `FleetView`, so simulations
//! drive it through scripted timelines with `now` as data (ARCHITECTURE §13).

use std::time::{Duration, Instant};

use crate::provider::PodId;
use crate::registry::{PodPhase, PodState};

#[derive(Debug, Clone)]
pub struct FleetView {
    pub pods: Vec<PodState>,
    pub queue_len: usize,
    pub warm_floor: u32,
    pub scale_up_fill: f32,
    pub cooldown: Duration,
    pub now: Instant,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScaleAction {
    Deploy { count: u32 },
    StartCooldown(PodId),
    CancelCooldown(PodId),
    Terminate(PodId),
    Replace(PodId),
    None,
}

fn ready_capacity(pods: &[PodState]) -> (u32, u32) {
    // (ready capacity, ready+used)
    let ready: Vec<&PodState> = pods.iter().filter(|p| p.state == PodPhase::Ready).collect();
    let cap: u32 = ready.iter().map(|p| p.cap).sum();
    let used: u32 = ready.iter().map(|p| p.fill).sum();
    (cap, used)
}

fn any_booting(pods: &[PodState]) -> bool {
    pods.iter().any(|p| p.state == PodPhase::Booting)
}

fn live_count(pods: &[PodState]) -> u32 {
    pods.iter()
        .filter(|p| !matches!(p.state, PodPhase::Dead))
        .count() as u32
}

/// The five rules (ARCHITECTURE §13), exactly:
///  1. Dead pods (stale ping) → Replace (terminate+deploy).
///  2. Preemptive scale-up at SCALE_UP_FILL (4/5 rule); never two Booting.
///  3. Queue backstop: queue_len > 0 and no Booting → Deploy{ceil(queue/cap)}.
///  4. Scale-down: Ready + fill==0 → cooldown; expiry → Terminate unless it
///     would drop live (ready+booting) below warm_floor.
///  5. Floor bootstrap: fewer live pods than warm_floor → Deploy.
pub fn decide(v: &FleetView) -> Vec<ScaleAction> {
    let mut actions = Vec::new();

    // Rule 1: stale pings → dead → replace.
    for p in &v.pods {
        if p.state != PodPhase::Dead
            && p.state != PodPhase::Booting
            && v.now.duration_since(p.last_ping) > PING_DEAD_AFTER
        {
            actions.push(ScaleAction::Replace(p.id.clone()));
        }
    }
    if !actions.is_empty() {
        // Replacements happen first; the rest of the policy re-evaluates on
        // the next tick after the new pod boots.
        return actions;
    }

    // Rule 5: floor bootstrap.
    let live = live_count(&v.pods);
    if live < v.warm_floor && !any_booting(&v.pods) {
        let need = v.warm_floor - live;
        actions.push(ScaleAction::Deploy { count: need });
        return actions;
    }

    // Rule 2: preemptive scale-up — fill threshold on READY capacity.
    let (ready_cap, ready_used) = ready_capacity(&v.pods);
    let fill_ratio = if ready_cap > 0 {
        ready_used as f32 / ready_cap as f32
    } else {
        0.0
    };
    if ready_cap > 0 && fill_ratio >= v.scale_up_fill && !any_booting(&v.pods) {
        actions.push(ScaleAction::Deploy { count: 1 });
        return actions;
    }

    // Rule 3: queue backstop.
    if v.queue_len > 0 && !any_booting(&v.pods) {
        let cap = v
            .pods
            .iter()
            .filter(|p| p.state == PodPhase::Ready)
            .map(|p| p.cap)
            .max()
            .unwrap_or(1)
            .max(1);
        let needed = (v.queue_len as u32).div_ceil(cap);
        actions.push(ScaleAction::Deploy { count: needed });
        return actions;
    }

    // Rule 4: scale-down with floor protection.
    let mut pending_terminations: u32 = 0;
    for p in &v.pods {
        if p.state == PodPhase::Ready && p.fill == 0 {
            match p.cooldown_until {
                Some(expiry) if expiry <= v.now => {
                    let live_after = live_count(&v.pods).saturating_sub(1 + pending_terminations);
                    if live_after >= v.warm_floor {
                        actions.push(ScaleAction::Terminate(p.id.clone()));
                        pending_terminations += 1;
                    }
                }
                None => actions.push(ScaleAction::StartCooldown(p.id.clone())),
                _ => {}
            }
        } else if p.state == PodPhase::Ready && p.fill > 0 && p.cooldown_until.is_some() {
            actions.push(ScaleAction::CancelCooldown(p.id.clone()));
        }
    }

    if actions.is_empty() {
        actions.push(ScaleAction::None);
    }
    actions
}

/// PING_DEAD_AFTER re-export for consistency (single source).
pub use crate::registry::PING_DEAD_AFTER;

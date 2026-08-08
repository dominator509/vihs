//! Pod registry (SPEC-002 registry keys; ARCHITECTURE §13).
//!
//! In-memory with Redis shadow for cross-restart visibility is overkill at
//! this stage: pods are ephemeral and re-register on boot (POST
//! /internal/pods/register). The registry owns the authoritative PodState
//! rows; the scaler and router read through it.

use std::collections::BTreeMap;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use dashmap::DashMap;

use crate::provider::PodId;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PodPhase {
    Booting,
    Ready,
    Draining,
    Dead,
}

impl PodPhase {
    pub fn is_ready(&self) -> bool {
        matches!(self, PodPhase::Ready)
    }
}

#[derive(Debug, Clone)]
pub struct PodState {
    pub id: PodId,
    pub addr: String,
    pub cap: u32,
    pub fill: u32,
    pub versions: Option<serde_json::Value>,
    pub last_ping: Instant,
    pub state: PodPhase,
    pub cooldown_until: Option<Instant>,
}

impl PodState {
    pub fn capacity_used(&self) -> f32 {
        if self.cap == 0 {
            0.0
        } else {
            self.fill as f32 / self.cap as f32
        }
    }
}

pub struct PodRegistry {
    pods: DashMap<String, PodState>,
    /// Ordered view for deterministic iteration (pod_id asc) — routers and
    /// admin snapshots iterate this, never the raw map. std::sync::RwLock:
    /// registry critical sections are tiny and called synchronously from
    /// async handlers (tokio's blocking_* panics inside a runtime).
    order: Arc<RwLock<BTreeMap<String, PodState>>>,
}

impl PodRegistry {
    pub fn new() -> Arc<Self> {
        Arc::new(PodRegistry {
            pods: DashMap::new(),
            order: Arc::new(RwLock::new(BTreeMap::new())),
        })
    }

    pub fn register(
        &self,
        id: &PodId,
        addr: String,
        cap: u32,
        versions: Option<serde_json::Value>,
    ) {
        let now = Instant::now();
        let state = PodState {
            id: id.clone(),
            addr,
            cap,
            fill: 0,
            versions,
            last_ping: now,
            state: PodPhase::Booting,
            cooldown_until: None,
        };
        self.pods.insert(id.as_str().to_string(), state.clone());
        let mut order = self.order.write().expect("registry lock");
        order.insert(id.as_str().to_string(), state);
    }

    pub fn ready(&self, id: &PodId) {
        if let Some(mut p) = self.pods.get_mut(id.as_str()) {
            p.state = PodPhase::Ready;
            p.last_ping = Instant::now();
            let snap = p.clone();
            drop(p);
            let mut order = self.order.write().expect("registry lock");
            order.insert(id.as_str().to_string(), snap);
        }
    }

    pub fn ping(&self, id: &PodId, fill: u32) {
        if let Some(mut p) = self.pods.get_mut(id.as_str()) {
            p.fill = fill.min(p.cap);
            p.last_ping = Instant::now();
            let snap = p.clone();
            drop(p);
            let mut order = self.order.write().expect("registry lock");
            order.insert(id.as_str().to_string(), snap);
        }
    }

    pub fn assign(&self, id: &PodId) {
        if let Some(mut p) = self.pods.get_mut(id.as_str()) {
            p.fill = (p.fill + 1).min(p.cap);
            p.cooldown_until = None; // any assignment cancels cooldown
            let snap = p.clone();
            drop(p);
            let mut order = self.order.write().expect("registry lock");
            order.insert(id.as_str().to_string(), snap);
        }
    }

    pub fn release(&self, id: &PodId) {
        if let Some(mut p) = self.pods.get_mut(id.as_str()) {
            p.fill = p.fill.saturating_sub(1);
            let snap = p.clone();
            drop(p);
            let mut order = self.order.write().expect("registry lock");
            order.insert(id.as_str().to_string(), snap);
        }
    }

    pub fn drain(&self, id: &PodId) {
        if let Some(mut p) = self.pods.get_mut(id.as_str()) {
            p.state = PodPhase::Draining;
            p.cooldown_until = None;
            let snap = p.clone();
            drop(p);
            let mut order = self.order.write().expect("registry lock");
            order.insert(id.as_str().to_string(), snap);
        }
    }

    pub fn start_cooldown(&self, id: &PodId, now: Instant, cooldown: Duration) {
        if let Some(mut p) = self.pods.get_mut(id.as_str()) {
            if p.state == PodPhase::Ready && p.fill == 0 {
                p.cooldown_until = Some(now + cooldown);
                let snap = p.clone();
                drop(p);
                let mut order = self.order.write().expect("registry lock");
                order.insert(id.as_str().to_string(), snap);
            }
        }
    }

    pub fn mark_dead(&self, id: &PodId) {
        if let Some(mut p) = self.pods.get_mut(id.as_str()) {
            p.state = PodPhase::Dead;
            let snap = p.clone();
            drop(p);
            let mut order = self.order.write().expect("registry lock");
            order.insert(id.as_str().to_string(), snap);
        }
    }

    pub fn remove(&self, id: &PodId) {
        self.pods.remove(id.as_str());
        let mut order = self.order.write().expect("registry lock");
        order.remove(id.as_str());
    }

    pub fn get(&self, id: &PodId) -> Option<PodState> {
        self.pods.get(id.as_str()).map(|p| p.clone())
    }

    /// Deterministic snapshot ordered by pod_id.
    pub fn snapshot(&self) -> Vec<PodState> {
        let order = self.order.read().expect("registry lock");
        order.values().cloned().collect()
    }
}

/// Stale-ping threshold: a pod that has not pinged for this long is Dead.
pub const PING_DEAD_AFTER: Duration = Duration::from_secs(15);

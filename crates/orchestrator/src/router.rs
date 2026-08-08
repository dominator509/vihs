//! Session → pod assignment (ARCHITECTURE §8; SPEC-003).
//!
//! `pick()` chooses the least-loaded Ready pod with fill < cap (hard cap
//! enforcement HERE — exceeding it spikes VRAM). Ties break by lowest pod_id
//! for determinism. None available → queue + `no_capacity{queued,eta_hint}`.

use std::sync::Arc;

use crate::error::OrchError;
use crate::memoryd_client::MemorydClient;
use crate::provider::{PodId, PodProvider};
use crate::registry::{PodPhase, PodRegistry};
use crate::tokens::{Scope, TokenStore, POD_TOKEN_TTL};

pub struct AssignOutcome {
    pub pod_id: PodId,
    pub addr: String,
    pub connection_id: String,
    pub memory_url: Option<String>,
    pub last_turn_id: u64,
    pub resume: bool,
}

/// Least-loaded Ready pod with fill < cap AND a LIVE assign channel (the
/// pod is only assignable while its assign WS is connected — a Ready state
/// with a dead channel is a stale registry entry). Deterministic: ties by
/// lowest pod_id (registry snapshot is already ordered).
pub fn pick(
    registry: &PodRegistry,
    cap: u32,
    channels: &crate::PodAssignChannels,
) -> Option<PodState> {
    registry
        .snapshot()
        .into_iter()
        .filter(|p| p.state == PodPhase::Ready && p.fill < p.cap.min(cap))
        .filter(|p| channels.senders.contains_key(p.id.as_str()))
        .min_by_key(|p| (p.fill, p.id.as_str().to_string()))
}

/// Full assignment: mint pod token + signed memory URL (via memoryd load),
/// bump fill, cancel cooldown, push the SPEC-003 `assign` frame on the pod's
/// internal WS. `resume` is derived from the DURABLE cursor (memoryd's
/// last_turn_id), never from the orchestrator's local session cache — the
/// cache can be stale (created turns:0, updated only by pod appends).
pub async fn assign(
    registry: &Arc<PodRegistry>,
    memoryd: &MemorydClient,
    assign_channels: &crate::PodAssignChannels,
    tokens: &TokenStore,
    session_id: &str,
    user_token: &str,
) -> Result<AssignOutcome, OrchError> {
    let pod = pick(registry, u32::MAX, assign_channels)
        .ok_or_else(|| OrchError::NoCapacity("no ready pod with capacity".into()))?;

    // memoryd load gives the cursor + signed memory URL.
    let load = memoryd.load(session_id, user_token).await?;
    let resume = load.last_turn_id > 0;
    let connection_id = uuid::Uuid::new_v4().to_string();
    // Real session-bound pod token (SPEC-005 A3): owner IS the session id,
    // TTL ≤ 15 min. memoryd enforces owner == path id + allowed verbs; the
    // pod uses this token for every memoryd call during the assignment.
    let pod_token = tokens.mint(session_id, Scope::Pod, POD_TOKEN_TTL).await?;

    // Liveness re-check before committing the assignment: if the channel
    // vanished between pick and send, return no-capacity (never a silent
    // 200 with a frame that went nowhere).
    if !assign_channels.senders.contains_key(pod.id.as_str()) {
        return Err(OrchError::NoCapacity("pod lost its assign channel".into()));
    }
    registry.assign(&pod.id);

    // Push the assign frame to the pod's internal WS (SPEC-003):
    // {"t":"assign","session_id","connection_id","memory_url",
    //  "audio_put_url","pod_token","resume":bool,"cursor":{...}}
    let frame = serde_json::json!({
        "t": "assign",
        "session_id": session_id,
        "connection_id": connection_id,
        "memory_url": load.memory_url,
        "pod_token": pod_token,
        "resume": resume,
        "cursor": {
            "tip_hash": load.tip_hash,
            "last_turn_id": load.last_turn_id,
            "epoch": load.epoch,
        },
    });
    if let Some(tx) = assign_channels.senders.get(pod.id.as_str()) {
        let _ = tx.send(frame);
    }

    Ok(AssignOutcome {
        pod_id: pod.id.clone(),
        addr: pod.addr.clone(),
        connection_id,
        memory_url: load.memory_url,
        last_turn_id: load.last_turn_id,
        resume,
    })
}

/// ETA hint for the queue path from the provider's cold-start hint.
pub fn eta_hint(provider: &dyn PodProvider) -> u64 {
    provider.cold_start_hint().as_secs()
}

/// Helper for queued state bookkeeping (no capacity, queued).
pub fn queued_eta(provider: &dyn PodProvider) -> (bool, u64) {
    (true, eta_hint(provider))
}

pub use crate::registry::PodState;

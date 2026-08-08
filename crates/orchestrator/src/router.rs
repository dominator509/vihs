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

pub struct AssignOutcome {
    pub pod_id: PodId,
    pub addr: String,
    pub connection_id: String,
    pub memory_url: Option<String>,
    pub last_turn_id: u64,
    pub resume: bool,
}

/// Least-loaded Ready pod with fill < cap. Deterministic: ties by lowest
/// pod_id (registry snapshot is already ordered).
pub fn pick(registry: &PodRegistry, cap: u32) -> Option<PodState> {
    registry
        .snapshot()
        .into_iter()
        .filter(|p| p.state == PodPhase::Ready && p.fill < p.cap.min(cap))
        .min_by_key(|p| (p.fill, p.id.as_str().to_string()))
}

/// Full assignment: mint pod token + signed memory URL (via memoryd load),
/// bump fill, cancel cooldown, push the SPEC-003 `assign` frame on the pod's
/// internal WS. Returns the frames the internal WS needs.
pub async fn assign(
    registry: &Arc<PodRegistry>,
    memoryd: &MemorydClient,
    assign_channels: &crate::PodAssignChannels,
    session_id: &str,
    user_token: &str,
    resume: bool,
) -> Result<AssignOutcome, OrchError> {
    let pod = pick(registry, u32::MAX)
        .ok_or_else(|| OrchError::NoCapacity("no ready pod with capacity".into()))?;

    // memoryd load gives the cursor + signed memory URL.
    let load = memoryd.load(session_id, user_token).await?;
    let connection_id = uuid::Uuid::new_v4().to_string();
    let pod_token = crate::pod_token();

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

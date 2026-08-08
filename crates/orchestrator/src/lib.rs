//! orchestrator — VIHS control plane (EP-004).
//!
//! Public API + admin API + internal pod API + signaling relay + router +
//! autoscaler against a mock pod provider (EP-009 swaps the real driver).

pub mod api_admin;
pub mod api_internal;
pub mod api_public;
pub mod authz;
pub mod client_static;
pub mod config;
pub mod error;
pub mod memoryd_client;
pub mod provider;
pub mod provider_mock;
pub mod queue;
pub mod registry;
pub mod router;
pub mod scaler;
pub mod session_index;
pub mod signal;
pub mod signal_route;

use std::collections::BTreeMap;
use std::sync::Arc;

use dashmap::DashMap;
use serde_json::Value;
use tokio::sync::{mpsc, Mutex};

use crate::authz::{Authorizer, PermissiveAuthorizer};
use crate::config::Config;
use crate::memoryd_client::MemorydClient;
use crate::provider::{PodId, PodProvider};
use crate::provider_mock::MockProvider;
use crate::queue::SessionQueue;
use crate::registry::PodRegistry;
use crate::session_index::SessionIndex;
use crate::signal::RelayHandle;

pub struct AppState {
    pub cfg: Config,
    pub memoryd: MemorydClient,
    pub registry: Arc<PodRegistry>,
    pub provider: Arc<dyn PodProvider>,
    pub sessions: Arc<SessionIndex>,
    pub queue: Arc<SessionQueue>,
    pub relay: Arc<RelayHandle>,
    /// pod_id → internal assign-channel sender (assign WS; SPEC-003 frames).
    pub pod_assign: Arc<PodAssignChannels>,
    pub authz: Box<dyn Authorizer>,
    pub minted_tokens: Arc<Mutex<BTreeMap<String, Value>>>,
    pub last_scale_decisions: Arc<Mutex<Vec<Value>>>,
}

/// Internal assign channels: the pod's `WS /internal/pods/{id}/assign`
/// handler registers a sender; the router pushes `assign`/`revoke` frames.
#[derive(Default)]
pub struct PodAssignChannels {
    pub senders: DashMap<String, mpsc::UnboundedSender<Value>>,
}

impl PodAssignChannels {
    pub fn new() -> Arc<Self> {
        Arc::new(PodAssignChannels::default())
    }
}

pub fn build_state(cfg: Config, memoryd: MemorydClient) -> Arc<AppState> {
    let provider: Arc<dyn PodProvider> = match cfg.provider.as_str() {
        "mock" => MockProvider::new(),
        other => {
            tracing::warn!("unknown PROVIDER {other}; falling back to mock");
            MockProvider::new()
        }
    };
    Arc::new(AppState {
        cfg,
        memoryd,
        registry: PodRegistry::new(),
        provider,
        sessions: SessionIndex::new(),
        queue: SessionQueue::new(),
        relay: RelayHandle::new(),
        pod_assign: PodAssignChannels::new(),
        authz: Box::new(PermissiveAuthorizer),
        minted_tokens: Arc::new(Mutex::new(BTreeMap::new())),
        last_scale_decisions: Arc::new(Mutex::new(Vec::new())),
    })
}

/// Pod token minted for an assignment (SPEC-003 internal assign frame).
pub fn pod_token() -> String {
    uuid::Uuid::new_v4().to_string()
}

pub fn make_pod_id() -> PodId {
    PodId::new()
}

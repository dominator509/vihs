//! orchestrator — VIHS control plane (EP-004).
//!
//! Public API + admin API + internal pod API + signaling relay + router +
//! autoscaler against a mock pod provider (EP-009 swaps the real driver).

pub mod api_admin;
pub mod api_internal;
pub mod api_public;
pub mod audit;
pub mod authz;
pub mod client_static;
pub mod config;
pub mod error;
pub mod memoryd_client;
pub mod metrics;
pub mod provider;
pub mod provider_mock;
pub mod provider_runpod;
pub mod queue;
pub mod ratelimit;
pub mod registry;
pub mod router;
pub mod scaler;
pub mod session_index;
pub mod signal;
pub mod signal_route;
pub mod tokens;

use std::collections::BTreeMap;
use std::sync::Arc;

use dashmap::DashMap;
use serde_json::Value;
use tokio::sync::{mpsc, Mutex};

use crate::authz::{Authorizer, TokenAuthorizer};
use crate::config::Config;
use crate::memoryd_client::MemorydClient;
use crate::provider::{PodId, PodProvider};
use crate::provider_mock::MockProvider;
use crate::queue::SessionQueue;
use crate::ratelimit::RateLimiter;
use crate::registry::PodRegistry;
use crate::session_index::SessionIndex;
use crate::signal::RelayHandle;
use crate::tokens::TokenStore;

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
    pub tokens: TokenStore,
    pub ratelimit: Arc<RateLimiter>,
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

pub async fn build_state(cfg: Config, memoryd: MemorydClient) -> Arc<AppState> {
    let provider: Arc<dyn PodProvider> = match cfg.provider.as_str() {
        "mock" => MockProvider::new(),
        "runpod" => match crate::provider_runpod::RunPodProvider::from_env() {
            Ok(p) => Arc::new(p),
            Err(e) => {
                tracing::error!("PROVIDER=runpod but driver init failed: {e}");
                std::process::exit(1);
            }
        },
        other => {
            tracing::warn!("unknown PROVIDER {other}; falling back to mock");
            MockProvider::new()
        }
    };

    // Token store: pepper from env; dev generates ephemeral with a loud log
    // (EP-006 §9 — stage/prod MUST set VIHS_TOKEN_PEPPER).
    let pepper = std::env::var("VIHS_TOKEN_PEPPER").unwrap_or_else(|_| {
        tracing::warn!(
            "VIHS_TOKEN_PEPPER unset — generating EPHEMERAL dev pepper; tokens will not survive restart"
        );
        uuid::Uuid::new_v4().to_string()
    });
    let tokens = TokenStore::connect(&cfg.redis_url, pepper)
        .await
        .expect("token store connect");

    // Seed env-provided credentials (EP-006 M1): the pod token (pod scope)
    // and an optional bootstrap admin token (admin scope) so the admin mint
    // route is reachable. Both must be 32-byte base64url strings — the store
    // rejects anything else loudly.
    let seed_ttl = std::time::Duration::from_secs(24 * 60 * 60);
    if let Ok(pod_tok) = std::env::var("VIHS_POD_TOKEN") {
        if !pod_tok.is_empty() {
            match tokens
                .seed(&pod_tok, "pod", crate::authz::Scope::Pod, seed_ttl)
                .await
            {
                Ok(()) => tracing::info!("seeded VIHS_POD_TOKEN (pod scope)"),
                Err(e) => tracing::error!("VIHS_POD_TOKEN not seeded: {e}"),
            }
        }
    }
    if let Ok(admin_tok) = std::env::var("VIHS_ADMIN_TOKEN") {
        if !admin_tok.is_empty() {
            match tokens
                .seed(
                    &admin_tok,
                    "bootstrap",
                    crate::authz::Scope::Admin,
                    seed_ttl,
                )
                .await
            {
                Ok(()) => tracing::info!("seeded VIHS_ADMIN_TOKEN (admin scope)"),
                Err(e) => tracing::error!("VIHS_ADMIN_TOKEN not seeded: {e}"),
            }
        }
    }

    // Warm the in-memory session cache from Redis (EP-007 M3 GAP-M3-4): the
    // orchestrator's session index is a cache over memoryd's durable owner
    // zsets. After a restart (required for token re-seed after Redis loss),
    // every surviving session would 404 without this warm-up.
    let sessions = SessionIndex::new();
    match sessions.warm_from_redis(&cfg.redis_url).await {
        Ok(n) => tracing::info!("session index warm-up: {n} sessions restored"),
        Err(e) => tracing::warn!("session index warm-up skipped: {e}"),
    }

    Arc::new(AppState {
        cfg,
        memoryd,
        registry: PodRegistry::new(),
        provider,
        sessions,
        queue: SessionQueue::new(),
        relay: RelayHandle::new(),
        pod_assign: PodAssignChannels::new(),
        authz: Box::new(TokenAuthorizer::new(tokens.clone())),
        tokens,
        ratelimit: Arc::new(RateLimiter::new()),
        minted_tokens: Arc::new(Mutex::new(BTreeMap::new())),
        last_scale_decisions: Arc::new(Mutex::new(Vec::new())),
    })
}

/// Pod token minting for an assignment is a REAL store mint (SPEC-005 A3:
/// session-bound, ≤15 min TTL) — see `router::assign`. There is deliberately
/// no bare-UUID fallback: an unverifiable token in the assign frame would
/// 401 every memoryd call the pod makes (EP-006 M3).
pub fn make_pod_id() -> PodId {
    PodId::new()
}

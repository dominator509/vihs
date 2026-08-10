//! Orchestrator admin API (SPEC-003 table; admin tokens, local only).

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::{header, HeaderMap},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::authz::{Scope, Verb};
use crate::error::OrchError;
use crate::AppState;

fn bearer(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(|s| s.to_string())
}

/// GET /admin/pods — registry rows: id, addr, state, fill/cap, ping age.
async fn pods(State(st): State<Arc<AppState>>, headers: HeaderMap) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    let p = st.authz.allow(&token, Verb::Admin).await?;
    if p.scope != Scope::Admin {
        return Err(OrchError::Authz("admin scope required".into()));
    }
    let now = std::time::Instant::now();
    let pods: Vec<Value> = st
        .registry
        .snapshot()
        .into_iter()
        .map(|pod| {
            let age = now.duration_since(pod.last_ping).as_secs();
            json!({
                "id": pod.id.as_str(),
                "addr": pod.addr,
                "state": match pod.state {
                    crate::registry::PodPhase::Booting => "booting",
                    crate::registry::PodPhase::Ready => "ready",
                    crate::registry::PodPhase::Draining => "draining",
                    crate::registry::PodPhase::Dead => "dead",
                },
                "fill": pod.fill,
                "cap": pod.cap,
                "last_ping_age_s": age,
                "versions": pod.versions,
            })
        })
        .collect();
    Ok(Json(json!({ "pods": pods })).into_response())
}

/// POST /admin/pods/{id}/drain — stop new assignments.
async fn drain_pod(
    State(st): State<Arc<AppState>>,
    Path(pod_id): Path<String>,
    headers: HeaderMap,
) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    let p = st.authz.allow(&token, Verb::Admin).await?;
    if p.scope != Scope::Admin {
        return Err(OrchError::Authz("admin scope required".into()));
    }
    let pod = st
        .registry
        .get(&crate::provider::PodId(pod_id.clone()))
        .ok_or_else(|| OrchError::NotFound(pod_id.clone()))?;
    st.registry.drain(&pod.id);
    Ok(Json(json!({ "draining": true, "pod_id": pod_id })).into_response())
}

/// GET /admin/scale — autoscaler state.
async fn scale(State(st): State<Arc<AppState>>, headers: HeaderMap) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    let p = st.authz.allow(&token, Verb::Admin).await?;
    if p.scope != Scope::Admin {
        return Err(OrchError::Authz("admin scope required".into()));
    }
    let pods = st.registry.snapshot();
    let fill: u32 = pods.iter().map(|p| p.fill).sum();
    let cap: u32 = pods.iter().map(|p| p.cap).sum();
    Ok(Json(json!({
        "fill": fill,
        "cap": cap,
        "queue_depth": st.queue.len().await,
        "warm_floor": st.cfg.warm_pool_floor,
        "last_decisions": st.last_scale_decisions.lock().await.clone(),
    }))
    .into_response())
}

#[derive(Deserialize)]
struct MintBody {
    owner_id: String,
    scope: String,
}

/// POST /admin/tokens — mint a user/admin token via the Redis token store
/// (SPEC-005 model; shown once). Admin-only; user tokens rejected (A6).
async fn mint_token(
    State(st): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(body): Json<MintBody>,
) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    let p = st.authz.allow(&token, Verb::Admin).await?;
    if p.scope != Scope::Admin {
        return Err(OrchError::Authz("admin scope required".into()));
    }
    let scope = match body.scope.as_str() {
        "admin" => crate::authz::Scope::Admin,
        "user" => crate::authz::Scope::User,
        other => return Err(OrchError::Invalid(format!("unknown scope {other}"))),
    };
    let minted = st
        .tokens
        .mint(&body.owner_id, scope, crate::tokens::DEFAULT_TOKEN_TTL)
        .await?;
    // Audit (ids only — the raw token is never logged; owner hashed).
    crate::audit::audit_mint("orchestrator", &body.owner_id, &body.scope);
    Ok(Json(json!({ "token": minted })).into_response())
}

pub fn admin_routes() -> Router<Arc<AppState>> {
    Router::new()
        .route("/admin/pods", get(pods))
        .route("/admin/pods/{id}/drain", post(drain_pod))
        .route("/admin/scale", get(scale))
        .route("/admin/tokens", post(mint_token))
}

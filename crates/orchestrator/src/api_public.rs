//! Orchestrator public API (SPEC-003 table; user tokens).

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::json;

use crate::authz::Verb;
use crate::error::OrchError;
use crate::queue::QueuedSession;
use crate::ratelimit::RateClass;
use crate::router;
use crate::session_index::SessionMeta;
use crate::AppState;

/// Derive the stable token id for rate-limit keying (never log the raw token).
fn rate_key(token: &str) -> String {
    vihs_auth::token_id(token).unwrap_or_else(|| token.to_string())
}

#[derive(Deserialize)]
struct CreateSessionBody {
    persona_id: String,
}

fn bearer(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(|s| s.to_string())
}

fn envelope(e: &OrchError) -> Response {
    (
        e.status(),
        Json(json!({
            "error": {"code": e.code(), "message": e.to_string(), "retryable": e.retryable()}
        })),
    )
        .into_response()
}

impl IntoResponse for OrchError {
    fn into_response(self) -> Response {
        envelope(&self)
    }
}

/// POST /v1/sessions — create. Persists the session row via memoryd's index
/// (owner zset) so the durable owner record exists, then registers locally.
async fn create_session(
    State(st): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(body): Json<CreateSessionBody>,
) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    let principal = st.authz.allow(&token, Verb::Session).await?;
    // SPEC-005 A5: session create ≤10/min per token → 429 rate_limited.
    st.ratelimit.check(&rate_key(&token), RateClass::Create)?;
    if body.persona_id.is_empty() {
        return Err(OrchError::Invalid("persona_id required".into()));
    }
    let session_id = uuid::Uuid::new_v4().to_string();
    let now = chrono::Utc::now().to_rfc3339();

    // Durable row in memoryd's Redis index (owner zset) — the orchestrator
    // index is a cache. create_session is a memoryd index op; the appending
    // of a persona note happens at first connect (pod path).
    st.memoryd
        .create_session(&session_id, &principal.owner, &now, &token)
        .await?;

    st.sessions
        .upsert(
            &principal.owner,
            SessionMeta {
                session_id: session_id.clone(),
                updated_at: now.clone(),
                turns: 0,
            },
        )
        .await;

    Ok((
        StatusCode::CREATED,
        Json(json!({
            "session_id": session_id,
            "created_at": now
        })),
    )
        .into_response())
}

/// POST /v1/sessions/{id}/resume | /connect — authorize owner → memoryd load
/// → assign with cursor + memory URL. No capacity → 503 queued.
async fn connect_session(
    State(st): State<Arc<AppState>>,
    Path(session_id): Path<String>,
    headers: HeaderMap,
) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    let principal = match st.authz.allow(&token, Verb::Session).await {
        Ok(p) => p,
        Err(e) => {
            crate::metrics::record_resume("denied");
            return Err(e);
        }
    };
    // SPEC-005 A5: resume ≤30/min per token → 429 rate_limited.
    if let Err(e) = st.ratelimit.check(&rate_key(&token), RateClass::Resume) {
        crate::metrics::record_resume("denied");
        return Err(e);
    }

    // Owner check + session must exist (404, never 403).
    let meta = match st.sessions.get(&principal.owner, &session_id).await {
        Some(m) => m,
        None => {
            crate::metrics::record_resume("denied");
            return Err(OrchError::NotFound(session_id.clone()));
        }
    };
    let _ = meta.turns; // display cache only — resume derives from the durable cursor

    match router::assign(
        &st.registry,
        &st.memoryd,
        &st.pod_assign,
        &st.tokens,
        &session_id,
        &token,
    )
    .await
    {
        Ok(outcome) => {
            crate::metrics::record_resume("ok");
            // Bind relay route so the client WS can reach the pod.
            st.relay
                .bind(&outcome.connection_id, outcome.pod_id.as_str(), &session_id)
                .await;
            Ok(Json(json!({
                "connect": {
                    "ws_url": format!("/v1/signal/{}", outcome.connection_id),
                    "connection_id": outcome.connection_id,
                    "pod_addr": outcome.addr,
                },
                "last_turn_id": outcome.last_turn_id,
                "memory_url": outcome.memory_url,
            }))
            .into_response())
        }
        Err(OrchError::NoCapacity(_)) => {
            crate::metrics::record_resume("error");
            let (queued, eta) = router::queued_eta(&*st.provider);
            st.queue
                .enqueue(QueuedSession {
                    session_id: session_id.clone(),
                    user_token: token,
                    queued_at: chrono::Utc::now(),
                })
                .await;
            Ok((
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({"error": {
                    "code": "no_capacity",
                    "message": "no ready pod",
                    "retryable": true,
                    "queued": queued,
                    "eta_hint_s": eta
                }})),
            )
                .into_response())
        }
        Err(e) => {
            crate::metrics::record_resume("error");
            Err(e)
        }
    }
}

/// GET /v1/sessions — owner's sessions, newest first.
async fn list_sessions(
    State(st): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    let principal = st.authz.allow(&token, Verb::Session).await?;
    let sessions = st.sessions.list(&principal.owner).await;
    Ok(Json(json!({ "sessions": sessions })).into_response())
}

/// GET /v1/sessions/{id}/transcript — streamed from memoryd render.
async fn transcript(
    State(st): State<Arc<AppState>>,
    Path(session_id): Path<String>,
    headers: HeaderMap,
) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    let principal = st.authz.allow(&token, Verb::Session).await?;
    st.sessions
        .get(&principal.owner, &session_id)
        .await
        .ok_or_else(|| OrchError::NotFound(session_id.clone()))?;
    let md = st.memoryd.transcript(&session_id, &token).await?;
    Ok(([(header::CONTENT_TYPE, "text/markdown")], md).into_response())
}

/// DELETE /v1/sessions/{id} — hard delete (D-9), idempotent.
async fn delete_session(
    State(st): State<Arc<AppState>>,
    Path(session_id): Path<String>,
    headers: HeaderMap,
) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    let principal = st.authz.allow(&token, Verb::Session).await?;
    st.sessions
        .get(&principal.owner, &session_id)
        .await
        .ok_or_else(|| OrchError::NotFound(session_id.clone()))?;
    st.memoryd.delete_session(&session_id, &token).await?;
    st.sessions.remove(&principal.owner, &session_id).await;
    st.queue.remove(&session_id).await;
    // SPEC-005 A7: hard delete is audited (ids only; owner hashed).
    crate::audit::audit_delete("orchestrator", &session_id, &principal.owner);
    Ok(StatusCode::NO_CONTENT.into_response())
}

pub fn public_routes() -> Router<Arc<AppState>> {
    Router::new()
        .route("/v1/sessions", post(create_session).get(list_sessions))
        .route("/v1/sessions/{id}/resume", post(connect_session))
        .route("/v1/sessions/{id}/connect", post(connect_session))
        .route("/v1/sessions/{id}/transcript", get(transcript))
        .route("/v1/sessions/{id}", axum::routing::delete(delete_session))
}

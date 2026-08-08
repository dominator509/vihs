//! memoryd HTTP API — routes exactly per SPEC-003 memoryd table.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::authz::{Authorizer, PermissiveAuthorizer, Verb};
use crate::compact::{
    maybe_compact, CompactConfig, Compacted, Deps as CompactDeps, MockSummarizer,
};
use crate::config::Config;
use crate::error::MemorydError;
use crate::index::RedisIndex;
use crate::store::{ObjectStore, S3Store, ARTIFACT_MEMORY, ARTIFACT_TRANSCRIPT};
use crate::writer::{AppendResult, WriterRegistry};
use vihs_auth::Scope;
use vihs_core::chain::fsck;
use vihs_core::event::Event;
use vihs_core::ids::SessionId;
use vihs_core::render::render_transcript;

pub struct ApiState {
    pub cfg: Config,
    pub store: Arc<S3Store>,
    pub index: Arc<RedisIndex>,
    pub registry: Arc<WriterRegistry>,
    pub authz: Box<dyn Authorizer>,
}

#[derive(Deserialize)]
struct EventBody {
    #[serde(flatten)]
    body: Value,
}

#[derive(Deserialize)]
struct _Unused; // placeholder for future empty-body handlers

fn bearer(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(|s| s.to_string())
}

fn error_response(e: MemorydError) -> Response {
    let (status, code, retryable) = match &e {
        MemorydError::Invalid(_) => (StatusCode::BAD_REQUEST, "invalid", false),
        // SPEC-006 mapping: missing/invalid token → 401, scope violation on a
        // non-session route → 403, foreign-or-unknown session → 404 (A4).
        MemorydError::Authz(crate::error::AuthzErr::Forbidden) => {
            (StatusCode::FORBIDDEN, "forbidden", false)
        }
        MemorydError::Authz(crate::error::AuthzErr::NotFound) => {
            (StatusCode::NOT_FOUND, "not_found", false)
        }
        // GAP-M3-5: an upstream (Redis) failure during token verification is
        // retryable — NEVER a 401, which would poison the client's credential
        // state on a transient outage (SPEC-006 retryable split).
        MemorydError::Authz(crate::error::AuthzErr::Upstream(_)) => {
            (StatusCode::SERVICE_UNAVAILABLE, "retryable", true)
        }
        MemorydError::Authz(_) => (StatusCode::UNAUTHORIZED, "unauthorized", false),
        MemorydError::IntegrityHold(_) => (StatusCode::CONFLICT, "integrity_hold", false),
        MemorydError::NotFound(_) => (StatusCode::NOT_FOUND, "not_found", false),
        MemorydError::Store(_) => (StatusCode::SERVICE_UNAVAILABLE, "retryable", true),
        MemorydError::Index(_) => (StatusCode::SERVICE_UNAVAILABLE, "retryable", true),
        MemorydError::Chain(_) => (StatusCode::CONFLICT, "integrity_hold", false),
    };
    (
        status,
        Json(json!({"error": {"code": code, "message": e.to_string(), "retryable": retryable}})),
    )
        .into_response()
}

impl IntoResponse for MemorydError {
    fn into_response(self) -> Response {
        error_response(self)
    }
}

async fn append_event(
    State(st): State<Arc<ApiState>>,
    Path(sid): Path<String>,
    headers: HeaderMap,
    Json(EventBody { body }): Json<EventBody>,
) -> Result<Response, MemorydError> {
    let sid = SessionId(sid);
    let token = bearer(&headers).unwrap_or_default();
    let principal = st.authz.allow(&token, &sid, Verb::Append).await?;
    // Create-path owner binding (SPEC-005 A1): the orchestrator's
    // session-create appends a system note with the caller's user token.
    // The index record may not exist yet (first append) OR may be a
    // heal-only record without an owner (rebuild/recovery — heal does not
    // touch owner). Bind the owner in BOTH cases so every later owner check
    // is meaningful. Pod tokens never stamp: a session's owner is the user,
    // never the session id itself.
    if !matches!(principal.scope, Scope::Pod) {
        let needs_stamp = match st.index.snapshot(&sid).await {
            Ok(s) => s.owner.as_deref() != Some(principal.owner.as_str()),
            Err(_) => true,
        };
        if needs_stamp {
            let now = chrono::Utc::now().to_rfc3339();
            st.index
                .create_session(&sid, &principal.owner, &now)
                .await?;
            tracing::debug!(
                sid = %sid.as_str(),
                owner_hash = %vihs_core::redact::owner_hash(&principal.owner),
                "authz create-path owner stamped"
            );
        } else {
            tracing::debug!(sid = %sid.as_str(), "authz record owner already bound");
        }
    }
    let handle = st.registry.get(&sid);
    match handle.append(body).await? {
        AppendResult::Committed { hash, turn_id } => Ok((
            StatusCode::OK,
            Json(json!({"status": "committed", "hash": hash, "turn_id": turn_id})),
        )
            .into_response()),
        AppendResult::Duplicate { hash } => Ok((
            StatusCode::OK,
            Json(json!({"status": "duplicate", "hash": hash})),
        )
            .into_response()),
        AppendResult::Rejected(msg) => Err(MemorydError::Invalid(msg)),
        AppendResult::Retryable(msg) => Err(MemorydError::Invalid(msg)),
    }
}

async fn load_session(
    State(st): State<Arc<ApiState>>,
    Path(sid): Path<String>,
    headers: HeaderMap,
) -> Result<Response, MemorydError> {
    let sid = SessionId(sid);
    let token = bearer(&headers).unwrap_or_default();
    st.authz.allow(&token, &sid, Verb::Load).await?;
    let snap = st
        .index
        .snapshot(&sid)
        .await
        .map_err(|_| MemorydError::NotFound(sid.to_string()))?;
    let bytes = st.store.read_log(&sid).await?;
    let values: Vec<Value> = bytes
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_slice(l).ok())
        .collect();
    let (_n, tip) = fsck(values.iter()).map_err(|e| MemorydError::IntegrityHold(e.to_string()))?;
    let memory_url = st
        .store
        .sign_get(
            &format!("sessions/{sid}/{ARTIFACT_MEMORY}"),
            std::time::Duration::from_secs(900),
        )
        .await
        .ok();
    Ok(Json(json!({
        "cursor": {
            "tip_hash": tip,
            "last_turn_id": snap.last_turn_id.unwrap_or(0),
            "epoch": snap.epoch.unwrap_or(0)
        },
        "memory_url": memory_url.map(|u| u.to_string())
    }))
    .into_response())
}

async fn get_memory(
    State(st): State<Arc<ApiState>>,
    Path(sid): Path<String>,
    headers: HeaderMap,
) -> Result<Response, MemorydError> {
    let sid = SessionId(sid);
    let token = bearer(&headers).unwrap_or_default();
    st.authz.allow(&token, &sid, Verb::Load).await?;
    st.index
        .snapshot(&sid)
        .await
        .map_err(|_| MemorydError::NotFound(sid.to_string()))?;
    match st.store.get_artifact(&sid, ARTIFACT_MEMORY).await {
        Ok(bytes) => Ok((
            [(axum::http::header::CONTENT_TYPE, "text/markdown")],
            bytes.to_vec(),
        )
            .into_response()),
        Err(_) => {
            // Render on demand (INV-5) when no artifact yet.
            let bytes = st.store.read_log(&sid).await?;
            let values: Vec<Value> = bytes
                .split(|b| *b == b'\n')
                .filter(|l| !l.is_empty())
                .filter_map(|l| serde_json::from_slice(l).ok())
                .collect();
            let events: Vec<Event> = values
                .iter()
                .filter_map(|v| serde_json::from_value(v.clone()).ok())
                .collect();
            Ok((
                [(axum::http::header::CONTENT_TYPE, "text/markdown")],
                render_transcript(&events).into_bytes(),
            )
                .into_response())
        }
    }
}

async fn get_transcript(
    State(st): State<Arc<ApiState>>,
    Path(sid): Path<String>,
    headers: HeaderMap,
) -> Result<Response, MemorydError> {
    let sid = SessionId(sid);
    let token = bearer(&headers).unwrap_or_default();
    st.authz.allow(&token, &sid, Verb::Load).await?;
    st.index
        .snapshot(&sid)
        .await
        .map_err(|_| MemorydError::NotFound(sid.to_string()))?;
    match st.store.get_artifact(&sid, ARTIFACT_TRANSCRIPT).await {
        Ok(bytes) => Ok((
            [(axum::http::header::CONTENT_TYPE, "text/markdown")],
            bytes.to_vec(),
        )
            .into_response()),
        Err(_) => {
            let bytes = st.store.read_log(&sid).await?;
            let values: Vec<Value> = bytes
                .split(|b| *b == b'\n')
                .filter(|l| !l.is_empty())
                .filter_map(|l| serde_json::from_slice(l).ok())
                .collect();
            let events: Vec<Event> = values
                .iter()
                .filter_map(|v| serde_json::from_value(v.clone()).ok())
                .collect();
            Ok((
                [(axum::http::header::CONTENT_TYPE, "text/markdown")],
                render_transcript(&events).into_bytes(),
            )
                .into_response())
        }
    }
}

async fn compact_session(
    State(st): State<Arc<ApiState>>,
    Path(sid): Path<String>,
    headers: HeaderMap,
) -> Result<Response, MemorydError> {
    let sid = SessionId(sid);
    let token = bearer(&headers).unwrap_or_default();
    st.authz.allow(&token, &sid, Verb::Append).await?;
    let deps = CompactDeps {
        store: st.store.clone(),
        index: st.index.clone(),
        registry: st.registry.clone(),
        cfg: CompactConfig {
            verbatim_tail: st.cfg.verbatim_tail,
            token_budget: st.cfg.token_budget,
        },
        summarizer: Box::new(MockSummarizer),
    };
    match maybe_compact(&sid, &deps).await? {
        Compacted::NotNeeded => Ok(Json(json!({"compacted": false})).into_response()),
        Compacted::Done { epoch, blob_tokens } => Ok(Json(
            json!({"compacted": true, "epoch": epoch, "blob_tokens": blob_tokens}),
        )
        .into_response()),
    }
}

async fn delete_session(
    State(st): State<Arc<ApiState>>,
    Path(sid): Path<String>,
    headers: HeaderMap,
) -> Result<Response, MemorydError> {
    let sid = SessionId(sid);
    let token = bearer(&headers).unwrap_or_default();
    let principal = st.authz.allow(&token, &sid, Verb::Delete).await?;
    st.index
        .snapshot(&sid)
        .await
        .map_err(|_| MemorydError::NotFound(sid.to_string()))?;
    crate::sweep::hard_delete(&sid, &st.store, &st.index, Some(&principal.owner)).await?;
    // SPEC-005 A7: hard delete is audited (ids only; owner hashed) — the
    // orchestrator also audits the user-facing DELETE; memoryd audits the
    // durable path (any caller, not just the orchestrator route).
    tracing::info!(
        target: "audit",
        line = %serde_json::json!({
            "ts": chrono::Utc::now().to_rfc3339(),
            "level": "info",
            "service": "memoryd",
            "event": "session_deleted",
            "fields": {
                "session_id": sid.as_str(),
                "owner_hash": vihs_core::redact::owner_hash(&principal.owner),
            }
        }).to_string(),
    );
    Ok(StatusCode::NO_CONTENT.into_response())
}

async fn healthz() -> &'static str {
    "ok"
}

/// SPEC-006 "Redis down: memoryd/orchestrator fail readyz" (EP-007 M3):
/// readyz pings Redis and returns 503 when the index is unreachable —
/// healthz stays a pure liveness probe, readyz is the dependency check.
async fn readyz(State(st): State<Arc<ApiState>>) -> Response {
    match st.index.ping().await {
        Ok(()) => (StatusCode::OK, "ok").into_response(),
        Err(e) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": {"code": "unavailable", "message": e.to_string(), "retryable": true}})),
        )
            .into_response(),
    }
}

pub fn router(state: Arc<ApiState>) -> Router {
    Router::new()
        .route("/v1/sessions/{sid}/events", post(append_event))
        .route("/v1/sessions/{sid}/load", post(load_session))
        .route("/v1/sessions/{sid}/memory", get(get_memory))
        .route("/v1/sessions/{sid}/transcript", get(get_transcript))
        .route("/v1/sessions/{sid}/compact", post(compact_session))
        .route("/v1/sessions/{sid}", axum::routing::delete(delete_session))
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .with_state(state)
}

/// Convenience used by storage-level integration tests: an ApiState with the
/// permissive dev authorizer (the strict TokenAuthorizer is wired in main.rs
/// and exercised by tests/authz.rs).
pub fn dev_state(
    cfg: Config,
    store: Arc<S3Store>,
    index: Arc<RedisIndex>,
    registry: Arc<WriterRegistry>,
) -> Arc<ApiState> {
    Arc::new(ApiState {
        cfg,
        store,
        index,
        registry,
        authz: Box::new(PermissiveAuthorizer),
    })
}

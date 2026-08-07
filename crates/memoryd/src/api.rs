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
    st.authz.allow(&token, &sid, Verb::Append)?;
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
    st.authz.allow(&token, &sid, Verb::Load)?;
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
    st.authz.allow(&token, &sid, Verb::Load)?;
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
    st.authz.allow(&token, &sid, Verb::Load)?;
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
    st.authz.allow(&token, &sid, Verb::Append)?;
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
    let principal = st.authz.allow(&token, &sid, Verb::Delete)?;
    st.index
        .snapshot(&sid)
        .await
        .map_err(|_| MemorydError::NotFound(sid.to_string()))?;
    crate::sweep::hard_delete(&sid, &st.store, &st.index, Some(&principal.owner)).await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

async fn healthz() -> &'static str {
    "ok"
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
        .route("/readyz", get(healthz))
        .with_state(state)
}

/// Convenience used by main: build an ApiState with default dev authz.
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

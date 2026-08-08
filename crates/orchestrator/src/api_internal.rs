//! Orchestrator internal API for pods (SPEC-003 table; pod tokens).
//!
//! register + health are HTTP; assignment is a WebSocket the pod connects
//! to (`WS /internal/pods/{id}/assign`) so the orchestrator can push
//! `assign`/`revoke` frames at any time.

use std::sync::Arc;

use axum::{
    extract::{
        ws::{Message, WebSocket},
        Path, State, WebSocketUpgrade,
    },
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::authz::Verb;
use crate::error::OrchError;
use crate::provider::PodId;
use crate::AppState;

fn bearer(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(|s| s.to_string())
}

#[derive(Deserialize)]
struct RegisterBody {
    pod_id: String,
    addr: String,
    cap: u32,
    versions: Option<Value>,
}

/// POST /internal/pods/register — pod boot.
async fn register_pod(
    State(st): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(body): Json<RegisterBody>,
) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    st.authz.allow(&token, Verb::Pod).await?;
    let id = PodId(body.pod_id.clone());
    st.registry.register(
        &id,
        body.addr.clone(),
        body.cap.max(1),
        body.versions.clone(),
    );
    Ok((
        StatusCode::CREATED,
        Json(json!({
            "registered": true,
            "pod_id": body.pod_id,
            "assign_ws": format!("/internal/pods/{}/assign", body.pod_id),
        })),
    )
        .into_response())
}

/// POST /internal/pods/{id}/health — 5 s ping.
async fn pod_health(
    State(st): State<Arc<AppState>>,
    Path(pod_id): Path<String>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Result<Response, OrchError> {
    let token = bearer(&headers).unwrap_or_default();
    st.authz.allow(&token, Verb::Pod).await?;
    let id = PodId(pod_id.clone());
    let fill = body["fill"].as_u64().unwrap_or(0) as u32;
    st.registry.ping(&id, fill);
    Ok((StatusCode::OK, Json(json!({"ok": true}))).into_response())
}

/// WS /internal/pods/{id}/assign — the pod holds this open; the orchestrator
/// pushes `assign`/`revoke` frames (SPEC-003 schema) whenever the router
/// assigns. The pod's initial `{"t":"ready"}` is acked so boot scripts can
/// synchronize on the channel being live.
async fn assign_ws(
    State(st): State<Arc<AppState>>,
    Path(pod_id): Path<String>,
    ws: WebSocketUpgrade,
) -> Response {
    ws.on_upgrade(move |socket| handle_assign_socket(socket, st, pod_id))
}

async fn handle_assign_socket(mut socket: WebSocket, st: Arc<AppState>, pod_id: String) {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Value>();
    st.pod_assign.senders.insert(pod_id.clone(), tx);

    // Drain the socket for the ready ping / close, and pump assign frames.
    loop {
        tokio::select! {
            maybe = socket.recv() => {
                match maybe {
                    Some(Ok(Message::Text(text))) => {
                        if let Ok(v) = serde_json::from_str::<Value>(&text) {
                            if v["t"] == "ready" {
                                // The pod's ready ping IS the readiness
                                // transition: stages up + assign channel live
                                // ⇒ eligible for assignment (SPEC-003).
                                st.registry.ready(&PodId(pod_id.clone()));
                                let _ = socket
                                    .send(Message::Text(
                                        json!({"t":"ack","pod_id":pod_id}).to_string().into(),
                                    ))
                                    .await;
                            }
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    _ => {}
                }
            }
            frame = rx.recv() => {
                match frame {
                    Some(v) => {
                        if socket
                            .send(Message::Text(v.to_string().into()))
                            .await
                            .is_err()
                        {
                            break;
                        }
                    }
                    None => break,
                }
            }
        }
    }
    st.pod_assign.senders.remove(&pod_id);
}

pub fn internal_routes() -> Router<Arc<AppState>> {
    Router::new()
        .route("/internal/pods/register", post(register_pod))
        .route("/internal/pods/{id}/health", post(pod_health))
        .route("/internal/pods/{id}/assign", get(assign_ws))
}

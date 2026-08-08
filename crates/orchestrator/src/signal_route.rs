//! Signaling WebSocket route: client ↔ orchestrator ↔ pod relay (SPEC-003).
//!
//! The client connects to `/v1/signal/{connection_id}`; the orchestrator
//! looks up which pod owns that connection (set at assignment time), opens a
//! pod-ward WS to `ws://{pod_addr}/internal/pods/{pod_id}/signal`, and pumps
//! frames both ways. `offer` goes pod-ward; `answer`/`ice` come back.
//! State frames are server→client.

use std::sync::Arc;

use axum::{
    extract::{
        ws::{Message, WebSocket},
        Path, State, WebSocketUpgrade,
    },
    http::{header, HeaderMap},
    response::{IntoResponse, Response},
};
use futures::{SinkExt, StreamExt};
use serde_json::{json, Value};

use crate::AppState;

fn bearer(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(|s| s.to_string())
}

/// WS /v1/signal/{connection_id} — the client's signaling channel.
pub async fn signal_socket(
    State(st): State<Arc<AppState>>,
    Path(connection_id): Path<String>,
    headers: HeaderMap,
    ws: WebSocketUpgrade,
) -> Response {
    let token = bearer(&headers).unwrap_or_default();
    // M2: real authz — the same user token used for the session API. The
    // connection_id itself is a capability (opaque UUID bound at assign),
    // but the bearer must still resolve to a valid principal (SPEC-005 A1).
    if let Err(e) = st.authz.allow(&token, crate::authz::Verb::Session).await {
        return axum::Json(json!({
            "error": {"code": e.code(), "message": e.to_string(), "retryable": e.retryable()}
        }))
        .into_response();
    }
    ws.on_upgrade(move |socket| handle_signal(socket, st, connection_id))
}

async fn handle_signal(mut client: WebSocket, st: Arc<AppState>, connection_id: String) {
    // Look up the pod that owns this connection (bound at assignment).
    let pod_conn = {
        let routes = st.relay.routes.lock().await;
        routes.get(&connection_id).cloned()
    };
    let Some(pod_key) = pod_conn else {
        let _ = client
            .send(Message::Text(
                json!({"t":"error","code":"not_found","message":"no assignment for connection"})
                    .to_string()
                    .into(),
            ))
            .await;
        return;
    };

    // Open a pod-ward signaling socket. In EP-004 the fake pod exposes
    // `/internal/pods/{id}/signal`; the real pod (EP-005/EP-009) mirrors it.
    let pod = st.registry.get(&crate::provider::PodId(pod_key.clone()));
    let Some(pod_state) = pod else {
        let _ = client
            .send(Message::Text(
                json!({"t":"error","code":"no_capacity","message":"pod gone"})
                    .to_string()
                    .into(),
            ))
            .await;
        return;
    };

    let ws_url = format!(
        "ws://{}/internal/pods/{pod_key}/signal?connection_id={connection_id}",
        pod_state.addr
    );
    let (pod_ws, _) = match tokio_tungstenite::connect_async(&ws_url).await {
        Ok(pair) => pair,
        Err(e) => {
            let _ = client
                .send(Message::Text(
                    json!({"t":"error","code":"upstream","message":e.to_string()})
                        .to_string()
                        .into(),
                ))
                .await;
            return;
        }
    };

    // Tell the client we're assigning (state frame).
    let _ = client
        .send(Message::Text(
            crate::signal::state_frame("assigning").to_string().into(),
        ))
        .await;

    // Two pump tasks: client→pod, pod→client. Stop when either side closes.
    let (mut client_sink, mut client_stream) = client.split();
    let (mut pod_sink, mut pod_stream) = pod_ws.split();

    // client → pod
    let c2p = tokio::spawn(async move {
        while let Some(Ok(msg)) = client_stream.next().await {
            let text = match msg {
                Message::Text(t) => t,
                _ => continue,
            };
            let v: Value = match serde_json::from_str(&text) {
                Ok(v) => v,
                Err(_) => continue,
            };
            // Forward validated client frames to the pod.
            if crate::signal::validate_client_frame(&v).is_ok() {
                let _ = pod_sink
                    .send(tokio_tungstenite::tungstenite::Message::Text(
                        text.to_string(),
                    ))
                    .await;
            }
        }
    });

    // pod → client (state + answer + ice + captions)
    while let Some(Ok(msg)) = pod_stream.next().await {
        let text = match msg {
            tokio_tungstenite::tungstenite::Message::Text(t) => t,
            tokio_tungstenite::tungstenite::Message::Close(_) => break,
            _ => continue,
        };
        if client_sink.send(Message::Text(text.into())).await.is_err() {
            break;
        }
    }
    c2p.abort();
    st.relay.unbind(&connection_id).await;
}

/// Static client route wiring lives in main.rs (client_static.rs serves the
/// embedded placeholder until EP-005 owns the real client).
pub fn client_routes() -> axum::Router<Arc<AppState>> {
    axum::Router::new().route(
        "/",
        axum::routing::get(|| async { crate::client_static::index_html() }),
    )
}

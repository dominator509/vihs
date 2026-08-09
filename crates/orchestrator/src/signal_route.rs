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
///
/// Two auth paths (SPEC-005: "WS uses header or first-message auth frame,
/// never query string"):
/// 1. Authorization header — used by the Python pod/harness clients, which
///    can set WS headers.
/// 2. First-message auth frame `{"t":"auth","token":"..."}` — the BROWSER
///    path: the WebSocket API cannot set headers, so the client sends the
///    token as its first frame. Validated in `handle_signal` BEFORE the
///    relay starts (M5); the frame is consumed, never forwarded to the pod.
pub async fn signal_socket(
    State(st): State<Arc<AppState>>,
    Path(connection_id): Path<String>,
    headers: HeaderMap,
    ws: WebSocketUpgrade,
) -> Response {
    let header_token = bearer(&headers);
    if let Some(token) = &header_token {
        // M2: real authz — the same user token used for the session API. The
        // connection_id itself is a capability (opaque UUID bound at assign),
        // but the bearer must still resolve to a valid principal (SPEC-005 A1).
        if let Err(e) = st.authz.allow(token, crate::authz::Verb::Session).await {
            return axum::Json(json!({
                "error": {"code": e.code(), "message": e.to_string(), "retryable": e.retryable()}
            }))
            .into_response();
        }
    }
    // No header → the handler awaits the first-message auth frame.
    ws.on_upgrade(move |socket| handle_signal(socket, st, connection_id, header_token))
}

/// Pure parse of a first-message auth frame (browser path). Returns the
/// token or a user-facing error. Extracted for unit testing.
fn parse_auth_frame(v: &Value) -> Result<String, String> {
    if v["t"] != "auth" {
        return Err(
            "first message must be an auth frame (browser WS cannot send headers)".to_string(),
        );
    }
    let token = v["token"]
        .as_str()
        .ok_or_else(|| "auth frame missing token".to_string())?;
    if token.is_empty() {
        return Err("auth frame missing token".to_string());
    }
    Ok(token.to_string())
}

/// Consume the first client message as an auth frame (browser path).
/// Returns the token, or a user-facing error string.
async fn first_auth_frame(client: &mut WebSocket) -> Result<String, String> {
    let msg = client.recv().await.ok_or("connection closed before auth")?;
    let msg = msg.map_err(|e| format!("auth frame read error: {e}"))?;
    match msg {
        Message::Text(t) => {
            let v: Value =
                serde_json::from_str(&t).map_err(|_| "first message must be JSON".to_string())?;
            parse_auth_frame(&v)
        }
        _ => {
            Err("first message must be an auth frame (browser WS cannot send headers)".to_string())
        }
    }
}

async fn handle_signal(
    mut client: WebSocket,
    st: Arc<AppState>,
    connection_id: String,
    header_token: Option<String>,
) {
    // Resolve the principal: header token (authz'd at upgrade) or the
    // first-message auth frame (browser path — SPEC-005).
    let token = match header_token {
        Some(t) => t,
        None => match first_auth_frame(&mut client).await {
            Ok(tok) => {
                if let Err(e) = st.authz.allow(&tok, crate::authz::Verb::Session).await {
                    let _ = client
                        .send(Message::Text(
                            json!({"t":"error","code":e.code(),"message":e.to_string(),"retryable":e.retryable()})
                                .to_string()
                                .into(),
                        ))
                        .await;
                    return;
                }
                tok
            }
            Err(msg) => {
                let _ = client
                    .send(Message::Text(
                        json!({"t":"error","code":"unauthenticated","message":msg,"retryable":false})
                            .to_string()
                            .into(),
                    ))
                    .await;
                return;
            }
        },
    };
    // Rate-limit key (SPEC-005 A5: signaling ≤50/s per token) — derived from
    // the stable token id, never the raw token (OBSERVABILITY redaction).
    let rate_key = vihs_auth::token_id(&token).unwrap_or_else(|| token.clone());

    // Look up the pod that owns this connection (bound at assignment).
    let pod_conn = {
        let routes = st.relay.routes.lock().await;
        routes.get(&connection_id).cloned()
    };
    let Some(route) = pod_conn else {
        let _ = client
            .send(Message::Text(
                json!({"t":"error","code":"not_found","message":"no assignment for connection"})
                    .to_string()
                    .into(),
            ))
            .await;
        return;
    };
    let pod_key = route.pod_id.clone();
    let session_id = route.session_id.clone();

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
        // The assignment was pushed but the pod vanished — revoke the slot
        // (best-effort; its assign channel is likely gone too).
        if let Some(tx) = st.pod_assign.senders.get(&pod_key) {
            let _ = tx.send(crate::signal::revoke_frame(&session_id, &connection_id));
        }
        st.relay.unbind(&connection_id).await;
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
            // The client can never reach the pod through this relay —
            // release the assignment slot so the session is reconnectable.
            if let Some(tx) = st.pod_assign.senders.get(&pod_key) {
                let _ = tx.send(crate::signal::revoke_frame(&session_id, &connection_id));
            }
            st.relay.unbind(&connection_id).await;
            return;
        }
    };

    // Tell the client we're assigning (state frame).
    let _ = client
        .send(Message::Text(
            crate::signal::state_frame("assigning").to_string().into(),
        ))
        .await;

    // Two pump tasks: client→pod, pod→client. tokio::select! runs them
    // concurrently and completes as soon as EITHER side closes — a
    // client-only close with a silent pod must still tear the relay down
    // (EP-007 M4) so the assignment is revoked and the pod's fill drains.
    let (client_sink, mut client_stream) = client.split();
    let (mut pod_sink, mut pod_stream) = pod_ws.split();
    // The rate-limit error frame must reach the client, so the client sink is
    // shared between the pumps (SPEC-005 A5 signaling ≤50/s per token).
    let client_sink = std::sync::Arc::new(tokio::sync::Mutex::new(client_sink));

    // client → pod
    let c2p_st = st.clone();
    let c2p_sink = client_sink.clone();
    let c2p = async move {
        // SPEC-006 signaling row: schema/size abuse → `bad_signal` error
        // frame, close the WS after 3 strikes (the live counterpart of
        // signal::client_read_loop's MAX_STRIKES; the loop here also does
        // rate limiting + pod forwarding).
        let mut strikes: u32 = 0;
        while let Some(Ok(msg)) = client_stream.next().await {
            let text = match msg {
                Message::Text(t) => t,
                _ => continue,
            };
            // Parse once; a JSON parse failure is itself a strike.
            let parsed: Option<Value> = serde_json::from_str(&text).ok();
            if let Err(reason) = crate::signal::strike_reason(&text, parsed.as_ref()) {
                strikes += 1;
                if strikes >= crate::signal::MAX_STRIKES {
                    // Third strike: `bad_signal` error frame + close
                    // (SPEC-006 row 6).
                    let _ = c2p_sink
                        .lock()
                        .await
                        .send(Message::Text(
                            json!({"t":"error","code":"bad_signal","message":reason,"retryable":false})
                                .to_string()
                                .into(),
                        ))
                        .await;
                    break;
                }
                continue;
            }
            // SPEC-005 A5: signaling messages ≤50/s per token → rate_limited
            // error frame + close (abuse control; SPEC-006 signaling row).
            if c2p_st
                .ratelimit
                .check(&rate_key, crate::ratelimit::RateClass::Signal)
                .is_err()
            {
                let _ = c2p_sink
                    .lock()
                    .await
                    .send(Message::Text(
                        json!({"t":"error","code":"rate_limited","message":"signaling rate limit exceeded","retryable":true})
                            .to_string()
                            .into(),
                    ))
                    .await;
                break;
            }
            // Forward validated client frames to the pod.
            let _ = pod_sink
                .send(tokio_tungstenite::tungstenite::Message::Text(
                    text.to_string(),
                ))
                .await;
        }
    };

    // pod → client (state + answer + ice + captions)
    let p2c_sink = client_sink.clone();
    let p2c = async move {
        while let Some(Ok(msg)) = pod_stream.next().await {
            let text = match msg {
                tokio_tungstenite::tungstenite::Message::Text(t) => t,
                tokio_tungstenite::tungstenite::Message::Close(_) => break,
                _ => continue,
            };
            if p2c_sink
                .lock()
                .await
                .send(Message::Text(text.into()))
                .await
                .is_err()
            {
                break;
            }
        }
    };

    tokio::select! {
        _ = c2p => {}
        _ = p2c => {}
    }

    // Either the client is gone or the pod side closed. Push a revoke frame
    // so the pod releases the assignment slot — the pod only pops
    // assignments on `revoke` (EP-007 M4: without this, fill never drains
    // and the next ramp stage gets no_capacity). Best-effort: the pod may
    // already be gone, in which case its assign channel is unregistered too.
    if let Some(tx) = st.pod_assign.senders.get(&pod_key) {
        let _ = tx.send(crate::signal::revoke_frame(&session_id, &connection_id));
    }
    st.relay.unbind(&connection_id).await;
}

/// Static client routes: `/` serves index.html, `/session.js` serves the F1
/// client script (EP-006 M5). `VIHS_CLIENT_DIR` overrides the embedded copy.
pub fn client_routes() -> axum::Router<Arc<AppState>> {
    axum::Router::new()
        .route(
            "/",
            axum::routing::get(|State(st): State<Arc<AppState>>| async move {
                axum::response::Html(crate::client_static::index_html(
                    st.cfg.client_dir.as_deref(),
                ))
            }),
        )
        .route(
            "/session.js",
            axum::routing::get(|State(st): State<Arc<AppState>>| async move {
                (
                    [(axum::http::header::CONTENT_TYPE, "text/javascript")],
                    crate::client_static::session_js(st.cfg.client_dir.as_deref()),
                )
            }),
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auth_frame_parses_valid_token() {
        let v = json!({"t":"auth","token":"tok-abc"});
        assert_eq!(parse_auth_frame(&v).unwrap(), "tok-abc");
    }

    #[test]
    fn auth_frame_rejects_non_auth_first_message() {
        // A client that tries offer WITHOUT auth first must be rejected —
        // the browser path requires the auth frame as the first message.
        let v = json!({"t":"offer","sdp":"v=0"});
        assert!(parse_auth_frame(&v).is_err());
    }

    #[test]
    fn auth_frame_rejects_missing_token() {
        let v = json!({"t":"auth"});
        assert!(parse_auth_frame(&v).is_err());
    }

    #[test]
    fn auth_frame_rejects_empty_token() {
        let v = json!({"t":"auth","token":""});
        assert!(parse_auth_frame(&v).is_err());
    }
}

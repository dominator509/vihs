//! M3 — end-to-end resume flow (ARCHITECTURE §8) over real HTTP + WS.
//!
//! Spins up the orchestrator app on an ephemeral port, a fake pod (register →
//! ready → assign WS → signal WS echo), then drives: create → connect
//! (assign frame received, fill bumped) → WS signal offer → fake pod echoes
//! answer → client receives it. Requires dev services (memoryd, Redis, MinIO).

use std::sync::Arc;

use axum::extract::ws::{Message, WebSocket};
use axum::{
    extract::{Path, State, WebSocketUpgrade},
    routing::get,
    Router,
};
use futures::{SinkExt, StreamExt};
use orchestrator::api_admin::admin_routes;
use orchestrator::api_internal::internal_routes;
use orchestrator::api_public::public_routes;
use orchestrator::config::Config as OrchConfig;
use orchestrator::memoryd_client::MemorydClient;
use orchestrator::registry::PodRegistry;
use orchestrator::{build_state, AppState};
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;

/// EP-006 M3: the strict network memoryd verifies tokens with the pepper in
/// .env (VIHS_TOKEN_PEPPER). The cargo-test process does NOT source .env, so
/// build_state would fall back to an ephemeral pepper and mint tokens the
/// memoryd service rejects. Inject the same pepper before building state.
fn ensure_shared_pepper() {
    if std::env::var("VIHS_TOKEN_PEPPER").is_ok() {
        return;
    }
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.env");
    if let Ok(text) = std::fs::read_to_string(&root) {
        for line in text.lines() {
            if let Some(v) = line.strip_prefix("VIHS_TOKEN_PEPPER=") {
                let v = v.trim();
                if !v.is_empty() {
                    std::env::set_var("VIHS_TOKEN_PEPPER", v);
                    return;
                }
            }
        }
    }
}

async fn orch_state() -> Arc<AppState> {
    ensure_shared_pepper();
    let mut cfg = OrchConfig::from_env();
    cfg.provider = "mock".to_string();
    cfg.warm_pool_floor = 1;
    let mem = MemorydClient::new(&cfg.memoryd_addr);
    build_state(cfg, mem).await
}

/// Mint a real user token via the store (EP-006 M1) for HTTP flows.
async fn mint_user(st: &Arc<AppState>, owner: &str) -> String {
    st.tokens
        .mint(
            owner,
            orchestrator::authz::Scope::User,
            std::time::Duration::from_secs(3600),
        )
        .await
        .expect("mint user token")
}

fn app(st: Arc<AppState>) -> Router {
    Router::new()
        .merge(public_routes())
        .merge(admin_routes())
        .merge(internal_routes())
        .route(
            "/v1/signal/{connection_id}",
            get(
                |State(st): State<Arc<AppState>>,
                 Path(connection_id): Path<String>,
                 headers: axum::http::HeaderMap,
                 ws: WebSocketUpgrade| async move {
                    orchestrator::signal_route::signal_socket(
                        State(st),
                        Path(connection_id),
                        headers,
                        ws,
                    )
                    .await
                },
            ),
        )
        .with_state(st)
}

/// The fake pod: registers, pings ready, holds the assign WS open, and
/// exposes `/internal/pods/{id}/signal` that echoes any frame back.
async fn fake_pod(
    st: Arc<AppState>,
    pod_id: &str,
) -> (
    String,
    tokio::task::JoinHandle<()>,
    tokio::sync::mpsc::UnboundedReceiver<Value>,
) {
    // Bind the echo server FIRST so the registry holds the real address.
    let echo_state = st.clone();
    let echo_pod = pod_id.to_string();
    let router = Router::new().route(
        "/internal/pods/{id}/signal",
        get(
            |State(st): State<Arc<AppState>>,
             Path(id): Path<String>,
             ws: WebSocketUpgrade| async move {
                ws.on_upgrade(move |socket| echo_loop(socket, st, id))
            },
        ),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let handle = tokio::spawn(async move {
        axum::serve(listener, router.with_state(echo_state))
            .await
            .expect("fake pod serve");
    });
    let _ = echo_pod;

    let registry: Arc<PodRegistry> = st.registry.clone();
    registry.register(
        &orchestrator::provider::PodId(pod_id.to_string()),
        addr.to_string(),
        2,
        Some(json!({"stages": ["mock"]})),
    );
    registry.ready(&orchestrator::provider::PodId(pod_id.to_string()));

    // The pod's assign channel (same map the WS handler populates).
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<Value>();
    st.pod_assign.senders.insert(pod_id.to_string(), tx.clone());

    (addr.to_string(), handle, rx)
}

async fn echo_loop(mut socket: WebSocket, _st: Arc<AppState>, _id: String) {
    while let Some(Ok(msg)) = socket.recv().await {
        if let Message::Text(text) = msg {
            // Echo back as an "answer" so the client sees a relayed frame.
            if let Ok(v) = serde_json::from_str::<Value>(&text) {
                let reply = if v["t"] == "offer" {
                    json!({"t": "answer", "sdp": "v=0 fake answer", "connection_id": v["connection_id"]})
                } else {
                    v.clone()
                };
                if socket
                    .send(Message::Text(reply.to_string().into()))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        } else if matches!(msg, Message::Close(_)) {
            break;
        }
    }
}

async fn http_json(
    base: &str,
    method: &str,
    path: &str,
    token: &str,
    body: Option<Value>,
) -> (u16, Value) {
    let client = reqwest::Client::new();
    let mut req = client.request(
        reqwest::Method::from_bytes(method.as_bytes()).unwrap(),
        format!("{base}{path}"),
    );
    if !token.is_empty() {
        req = req.bearer_auth(token);
    }
    if let Some(b) = body {
        req = req.json(&b);
    }
    let resp = req.send().await.unwrap();
    let status = resp.status().as_u16();
    let text = resp.text().await.unwrap();
    let v = serde_json::from_str(&text).unwrap_or_else(|_| json!({"_raw": text}));
    (status, v)
}

#[tokio::test]
async fn fresh_session_full_flow() {
    let st = orch_state().await;
    let user_tok = mint_user(&st, "owner-flow").await;
    let app = app(st.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let serve = tokio::spawn(async move {
        axum::serve(listener, app).await.expect("serve test app");
    });
    let base = format!("http://{addr}");

    // Fake pod registers itself.
    let pod_id = format!("pod-{}", uuid::Uuid::new_v4());
    let (_pod_addr, _handle, mut assign_rx) = fake_pod(st.clone(), &pod_id).await;

    // Create a session (durable row in memoryd).
    let (status, created) = http_json(
        &base,
        "POST",
        "/v1/sessions",
        &user_tok,
        Some(json!({"persona_id": "persona-a"})),
    )
    .await;
    assert_eq!(status, 201, "{created}");
    let sid = created["session_id"].as_str().unwrap().to_string();

    // Connect (fresh: turns == 0 → resume=false).
    let (status, connect) = http_json(
        &base,
        "POST",
        &format!("/v1/sessions/{sid}/connect"),
        &user_tok,
        None,
    )
    .await;
    assert_eq!(status, 200, "{connect}");
    assert!(connect["connect"]["ws_url"].is_string());
    assert!(connect["connect"]["connection_id"].is_string());
    assert_eq!(connect["last_turn_id"], 0, "fresh session cursor");

    // The assign frame must have reached the pod's internal channel.
    let frame = tokio::time::timeout(std::time::Duration::from_secs(2), assign_rx.recv())
        .await
        .expect("assign frame timeout")
        .expect("channel closed");
    assert_eq!(frame["t"], "assign");
    assert_eq!(frame["session_id"], sid);
    assert_eq!(frame["resume"], false);

    // WS signaling: connect to /v1/signal/{connection_id}, send an offer,
    // expect the fake pod's answer echoed back.
    let connection_id = connect["connect"]["connection_id"].as_str().unwrap();
    let ws_url = format!("ws://{addr}/v1/signal/{connection_id}");
    let mut req = ws_url.into_client_request().unwrap();
    req.headers_mut().insert(
        axum::http::header::AUTHORIZATION,
        format!("Bearer {user_tok}").parse().unwrap(),
    );
    let (mut ws, _) = tokio_tungstenite::connect_async(req)
        .await
        .expect("ws connect");

    ws.send(tokio_tungstenite::tungstenite::Message::Text(
        json!({"t": "offer", "sdp": "v=0 client offer"}).to_string(),
    ))
    .await
    .expect("send offer");

    // The relay may send state frames ("assigning") before the pod's answer.
    let mut answer: Option<Value> = None;
    for _ in 0..5 {
        let reply = tokio::time::timeout(std::time::Duration::from_secs(3), ws.next())
            .await
            .expect("reply timeout")
            .expect("stream closed")
            .expect("ws error");
        let text = match reply {
            tokio_tungstenite::tungstenite::Message::Text(t) => t,
            other => panic!("expected text reply, got {other:?}"),
        };
        let v: Value = serde_json::from_str(&text).unwrap();
        if v["t"] == "answer" {
            answer = Some(v);
            break;
        }
    }
    let v = answer.expect("fake pod answer must arrive");
    assert_eq!(v["sdp"], "v=0 fake answer");

    ws.close(None).await.ok();
    serve.abort();
    st.pod_assign.senders.remove(&pod_id);
}

#[tokio::test]
async fn cap_enforcement_queues_second_connect() {
    let st = orch_state().await;
    let user_tok = mint_user(&st, "owner-cap").await;
    let app = app(st.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let serve = tokio::spawn(async move {
        axum::serve(listener, app).await.expect("serve test app");
    });
    let base = format!("http://{addr}");

    // Fake pod with cap=1.
    let pod_id = format!("pod-{}", uuid::Uuid::new_v4());
    let registry = st.registry.clone();
    registry.register(
        &orchestrator::provider::PodId(pod_id.clone()),
        "127.0.0.1:0".to_string(),
        1,
        None,
    );
    registry.ready(&orchestrator::provider::PodId(pod_id.clone()));
    let (tx, _rx) = tokio::sync::mpsc::unbounded_channel::<Value>();
    st.pod_assign.senders.insert(pod_id.clone(), tx);

    let (status, created) = http_json(
        &base,
        "POST",
        "/v1/sessions",
        &user_tok,
        Some(json!({"persona_id": "persona-a"})),
    )
    .await;
    assert_eq!(status, 201);
    let sid = created["session_id"].as_str().unwrap().to_string();

    // First connect fills the pod.
    let (status, _) = http_json(
        &base,
        "POST",
        &format!("/v1/sessions/{sid}/connect"),
        &user_tok,
        None,
    )
    .await;
    assert_eq!(status, 200);

    // Second connect to the same session → no ready pod with capacity →
    // 503 queued (SPEC-003 acceptance).
    let (status, body) = http_json(
        &base,
        "POST",
        &format!("/v1/sessions/{sid}/connect"),
        &user_tok,
        None,
    )
    .await;
    assert_eq!(status, 503, "{body}");
    assert_eq!(body["error"]["code"], "no_capacity");
    assert_eq!(body["error"]["queued"], true);

    serve.abort();
    st.pod_assign.senders.remove(&pod_id);
}

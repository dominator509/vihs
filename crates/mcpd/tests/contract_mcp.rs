//! MCP contract tests (SPEC-003 §MCP) — JSON-RPC fixtures mirroring the
//! route contract tests. Spawns a real orchestrator HTTP listener (dev
//! services required for session ops), then drives mcpd's JSON-RPC surface
//! over tower oneshot: initialize, tools/list, tools/call per tool.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, HeaderMap, Request};
use axum::Router;
use http_body_util::BodyExt;
use mcpd::{make_state, McpdState};
use orchestrator::api_admin::admin_routes;
use orchestrator::api_internal::internal_routes;
use orchestrator::api_public::public_routes;
use orchestrator::config::Config as OrchConfig;
use orchestrator::memoryd_client::MemorydClient;
use orchestrator::{build_state, AppState};
use serde_json::{json, Value};
use tower::ServiceExt;

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

fn orch_app(st: Arc<AppState>) -> Router {
    Router::new()
        .merge(public_routes())
        .merge(admin_routes())
        .merge(internal_routes())
        .with_state(st)
}

async fn rpc(state: Arc<McpdState>, headers: HeaderMap, req: Value) -> Value {
    let app = Router::new()
        .route("/", axum::routing::post(mcpd::handle_rpc))
        .with_state(state);
    let mut builder = Request::builder()
        .method("POST")
        .uri("/")
        .header(header::CONTENT_TYPE, "application/json");
    for (k, v) in headers.iter() {
        builder = builder.header(k, v);
    }
    let request = builder.body(Body::from(req.to_string())).unwrap();
    let resp = app.clone().oneshot(request).await.unwrap();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap()
}

fn bearer(tok: &str) -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(
        header::AUTHORIZATION,
        format!("Bearer {tok}").parse().unwrap(),
    );
    h
}

async fn spawn_orchestrator() -> (String, Arc<AppState>) {
    let st = orch_state().await;
    let app = orch_app(st.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.expect("serve orch");
    });
    (format!("http://{addr}"), st)
}

/// Start the orchestrator + mcpd and mint real user/admin tokens (EP-006 M1).
async fn setup() -> (Arc<McpdState>, String, String, String) {
    let (orch_base, st) = spawn_orchestrator().await;
    let user_tok = st
        .tokens
        .mint(
            "owner-mcp",
            orchestrator::authz::Scope::User,
            std::time::Duration::from_secs(3600),
        )
        .await
        .expect("mint user");
    let admin_tok = st
        .tokens
        .mint(
            "root-mcp",
            orchestrator::authz::Scope::Admin,
            std::time::Duration::from_secs(3600),
        )
        .await
        .expect("mint admin");
    let state = make_state(orch_base.clone());
    (state, orch_base, user_tok, admin_tok)
}

#[tokio::test]
async fn initialize_returns_protocol_capabilities() {
    let (state, _, _, _) = setup().await;
    let resp = rpc(
        state,
        HeaderMap::new(),
        json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}),
    )
    .await;
    assert_eq!(resp["jsonrpc"], "2.0");
    assert_eq!(resp["id"], 1);
    assert!(resp["result"]["protocolVersion"].is_string());
    assert_eq!(resp["result"]["capabilities"]["tools"], json!({}));
    assert!(resp["result"]["serverInfo"]["name"].is_string());
}

#[tokio::test]
async fn tools_list_has_nine_vihs_tools() {
    let (state, _, _, _) = setup().await;
    let resp = rpc(
        state,
        HeaderMap::new(),
        json!({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}),
    )
    .await;
    let tools = resp["result"]["tools"].as_array().unwrap();
    assert_eq!(tools.len(), 9, "SPEC-003 §MCP lists 9 tools");
    let names: Vec<&str> = tools.iter().map(|t| t["name"].as_str().unwrap()).collect();
    for expected in [
        "vihs_session_create",
        "vihs_session_resume",
        "vihs_session_list",
        "vihs_session_transcript",
        "vihs_session_delete",
        "vihs_pods_list",
        "vihs_pod_drain",
        "vihs_scale_status",
        "vihs_token_mint",
    ] {
        assert!(
            names.contains(&expected),
            "missing tool {expected}: {names:?}"
        );
    }
    for t in tools {
        assert!(t["inputSchema"].is_object());
        assert!(t["description"].is_string());
    }
}

#[tokio::test]
async fn unknown_tool_returns_jsonrpc_error() {
    let (state, _, _, _) = setup().await;
    let resp = rpc(
        state,
        HeaderMap::new(),
        json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"vihs_nope","arguments":{}}}),
    )
    .await;
    assert!(resp["error"].is_object(), "{resp}");
    assert_eq!(resp["error"]["code"], -32602);
}

#[tokio::test]
async fn unknown_method_returns_error() {
    let (state, _, _, _) = setup().await;
    let resp = rpc(
        state,
        HeaderMap::new(),
        json!({"jsonrpc":"2.0","id":4,"method":"bogus/method","params":{}}),
    )
    .await;
    assert_eq!(resp["error"]["code"], -32601);
}

#[tokio::test]
async fn session_create_tool_mirrors_route_contract() {
    let (state, _, user_tok, _) = setup().await;
    let resp = rpc(
        state,
        bearer(&user_tok),
        json!({"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"vihs_session_create","arguments":{"persona_id":"persona-mcp"}}}),
    )
    .await;
    assert!(!resp["result"]["isError"].as_bool().unwrap(), "{resp}");
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let payload: Value = serde_json::from_str(text).unwrap();
    assert!(payload["session_id"].is_string());
    assert!(payload["created_at"].is_string());
}

#[tokio::test]
async fn session_create_missing_arg_is_error() {
    let (state, _, user_tok, _) = setup().await;
    let resp = rpc(
        state,
        bearer(&user_tok),
        json!({"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"vihs_session_create","arguments":{}}}),
    )
    .await;
    assert_eq!(resp["error"]["code"], -32602, "{resp}");
}

#[tokio::test]
async fn session_resume_unknown_404_surfaces_is_error() {
    let (state, _, user_tok, _) = setup().await;
    let resp = rpc(
        state,
        bearer(&user_tok),
        json!({"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"vihs_session_resume","arguments":{"session_id":"ghost-session"}}}),
    )
    .await;
    let result = &resp["result"];
    assert!(result["isError"].as_bool().unwrap(), "{resp}");
    // SPEC-006 envelope passes through in data.error.
    let err = &result["data"]["error"]["error"];
    assert_eq!(err["code"], "not_found", "{resp}");
}

#[tokio::test]
async fn session_list_tool_contract() {
    let (state, _, user_tok, _) = setup().await;
    let resp = rpc(
        state,
        bearer(&user_tok),
        json!({"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"vihs_session_list","arguments":{}}}),
    )
    .await;
    assert!(!resp["result"]["isError"].as_bool().unwrap(), "{resp}");
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let payload: Value = serde_json::from_str(text).unwrap();
    assert!(payload["sessions"].is_array());
}

#[tokio::test]
async fn pods_list_tool_contract() {
    let (state, _, _, admin_tok) = setup().await;
    let resp = rpc(
        state,
        bearer(&admin_tok),
        json!({"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"vihs_pods_list","arguments":{}}}),
    )
    .await;
    assert!(!resp["result"]["isError"].as_bool().unwrap(), "{resp}");
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let payload: Value = serde_json::from_str(text).unwrap();
    assert!(payload["pods"].is_array());
}

#[tokio::test]
async fn scale_status_tool_contract() {
    let (state, _, _, admin_tok) = setup().await;
    let resp = rpc(
        state,
        bearer(&admin_tok),
        json!({"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"vihs_scale_status","arguments":{}}}),
    )
    .await;
    assert!(!resp["result"]["isError"].as_bool().unwrap(), "{resp}");
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let payload: Value = serde_json::from_str(text).unwrap();
    assert!(payload["fill"].is_u64());
    assert!(payload["warm_floor"].is_u64());
    assert!(payload["queue_depth"].is_u64());
}

#[tokio::test]
async fn token_mint_tool_contract() {
    let (state, _, _, admin_tok) = setup().await;
    let resp = rpc(
        state,
        bearer(&admin_tok),
        json!({"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"vihs_token_mint","arguments":{"owner_id":"owner-mcp","scope":"user"}}}),
    )
    .await;
    assert!(!resp["result"]["isError"].as_bool().unwrap(), "{resp}");
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let payload: Value = serde_json::from_str(text).unwrap();
    assert!(payload["token"].is_string() && !payload["token"].as_str().unwrap().is_empty());
}

#[tokio::test]
async fn pod_drain_tool_contract() {
    let (state, _, _, admin_tok) = setup().await;
    // Register a pod first so drain has a target.
    let _ = rpc(
        state.clone(),
        bearer("pod-tok"),
        json!({"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"vihs_pods_list","arguments":{}}}),
    )
    .await;
    // Drain an unknown pod → isError true (route 404).
    let resp = rpc(
        state,
        bearer(&admin_tok),
        json!({"jsonrpc":"2.0","id":13,"method":"tools/call","params":{"name":"vihs_pod_drain","arguments":{"pod_id":"ghost-pod"}}}),
    )
    .await;
    let result = &resp["result"];
    assert!(result["isError"].as_bool().unwrap(), "{resp}");
    assert_eq!(result["data"]["error"]["error"]["code"], "not_found");
}

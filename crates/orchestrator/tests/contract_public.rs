//! M3 — public API contract tests (SPEC-003 route registry).
//!
//! Each route's response schema asserted via tower oneshot — no network.
//! The session-scoped routes hit memoryd for the durable row, so dev
//! services must be up (test-integration.sh gate). EP-006 M1: tokens are
//! minted from the real Redis store (no permissive mode in these tests).

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{header, HeaderMap, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use orchestrator::api_admin::admin_routes;
use orchestrator::api_internal::internal_routes;
use orchestrator::api_public::public_routes;
use orchestrator::authz::Scope;
use orchestrator::client_static;
use orchestrator::config::Config as OrchConfig;
use orchestrator::memoryd_client::MemorydClient;
use orchestrator::provider::PodId;
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

async fn state() -> Arc<AppState> {
    ensure_shared_pepper();
    let mut cfg = OrchConfig::from_env();
    cfg.provider = "mock".to_string();
    cfg.warm_pool_floor = 1;
    let mem = MemorydClient::new(&cfg.memoryd_addr);
    build_state(cfg, mem).await
}

fn app(st: Arc<AppState>) -> Router {
    Router::new()
        .merge(public_routes())
        .merge(admin_routes())
        .merge(internal_routes())
        .route("/healthz", axum::routing::get(|| async { "ok" }))
        .with_state(st)
}

/// Mint a real token via the store (EP-006 M1). Owner scoping matters for
/// the list/resume flows, so tests mint per-owner tokens.
async fn mint(st: &Arc<AppState>, owner: &str, scope: Scope) -> String {
    st.tokens
        .mint(owner, scope, Duration::from_secs(3600))
        .await
        .expect("mint")
}

fn bearer(tok: &str) -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(
        header::AUTHORIZATION,
        format!("Bearer {tok}").parse().unwrap(),
    );
    h
}

async fn send_json(
    app: &Router,
    method: &str,
    uri: &str,
    headers: HeaderMap,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let mut builder = Request::builder().method(method).uri(uri);
    for (k, v) in headers.iter() {
        builder = builder.header(k, v);
    }
    if body.is_some() {
        builder = builder.header(header::CONTENT_TYPE, "application/json");
    }
    let req = builder
        .body(Body::from(body.map(|b| b.to_string()).unwrap_or_default()))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let text = String::from_utf8_lossy(&bytes).to_string();
    let v = serde_json::from_str(&text).unwrap_or_else(|_| json!({"_raw": text}));
    (status, v)
}

#[tokio::test]
async fn healthz_is_ok() {
    let st = state().await;
    let app = app(st);
    let (status, _) = send_json(&app, "GET", "/healthz", HeaderMap::new(), None).await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn create_session_returns_contract() {
    let st = state().await;
    let tok = mint(&st, "owner-create", Scope::User).await;
    let app = app(st);
    let (status, body) = send_json(
        &app,
        "POST",
        "/v1/sessions",
        bearer(&tok),
        Some(json!({"persona_id": "persona-a"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert!(body["session_id"].is_string(), "contract: {body}");
    assert!(body["created_at"].is_string());
}

#[tokio::test]
async fn create_session_requires_persona() {
    let st = state().await;
    let tok = mint(&st, "owner-create", Scope::User).await;
    let app = app(st);
    let (status, body) = send_json(
        &app,
        "POST",
        "/v1/sessions",
        bearer(&tok),
        Some(json!({"persona_id": ""})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "invalid");
}

#[tokio::test]
async fn create_session_requires_token() {
    let st = state().await;
    let app = app(st);
    let (status, body) = send_json(
        &app,
        "POST",
        "/v1/sessions",
        HeaderMap::new(),
        Some(json!({"persona_id": "persona-a"})),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{body}");
}

#[tokio::test]
async fn list_sessions_empty_contract() {
    let st = state().await;
    let tok = mint(&st, "owner-list", Scope::User).await;
    let app = app(st);
    let (status, body) = send_json(&app, "GET", "/v1/sessions", bearer(&tok), None).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["sessions"].is_array(), "contract: {body}");
}

#[tokio::test]
async fn list_sessions_shows_created() {
    let st = state().await;
    // Unique owner per run: the dev Redis is shared across runs and the
    // session index is warmed from owner zsets at startup (EP-007 M3
    // GAP-M3-4), so a fixed owner would see residue from earlier runs.
    let owner = format!(
        "owner-list-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let tok = mint(&st, &owner, Scope::User).await;
    let app = app(st);
    let (_, _) = send_json(
        &app,
        "POST",
        "/v1/sessions",
        bearer(&tok),
        Some(json!({"persona_id": "persona-a"})),
    )
    .await;
    let (_, body) = send_json(&app, "GET", "/v1/sessions", bearer(&tok), None).await;
    assert_eq!(body["sessions"].as_array().unwrap().len(), 1, "{body}");
    let row = &body["sessions"][0];
    assert!(row["session_id"].is_string());
    assert!(row["updated_at"].is_string());
    assert!(row["turns"].is_u64());
}

#[tokio::test]
async fn resume_unknown_session_404_not_403() {
    let st = state().await;
    let tok = mint(&st, "owner-resume", Scope::User).await;
    let app = app(st);
    let (status, body) = send_json(
        &app,
        "POST",
        "/v1/sessions/does-not-exist/resume",
        bearer(&tok),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "404, never 403: {body}");
    assert_eq!(body["error"]["code"], "not_found");
}

#[tokio::test]
async fn resume_no_capacity_queues_503() {
    let st = state().await;
    let tok = mint(&st, "owner-resume", Scope::User).await;
    let app = app(st);
    // Create a session, then resume with NO pods registered → must queue 503.
    let (status, created) = send_json(
        &app,
        "POST",
        "/v1/sessions",
        bearer(&tok),
        Some(json!({"persona_id": "persona-a"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let sid = created["session_id"].as_str().unwrap();

    let (status, body) = send_json(
        &app,
        "POST",
        format!("/v1/sessions/{sid}/resume").as_str(),
        bearer(&tok),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(body["error"]["code"], "no_capacity");
    assert_eq!(body["error"]["queued"], true);
    assert!(body["error"]["eta_hint_s"].is_u64());
}

#[tokio::test]
async fn delete_unknown_session_404() {
    let st = state().await;
    let tok = mint(&st, "owner-del", Scope::User).await;
    let app = app(st);
    let (status, _) = send_json(&app, "DELETE", "/v1/sessions/nope", bearer(&tok), None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn admin_pods_contract() {
    let st = state().await;
    let tok = mint(&st, "root", Scope::Admin).await;
    let app = app(st);
    let (status, body) = send_json(&app, "GET", "/admin/pods", bearer(&tok), None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body["pods"].is_array());
}

#[tokio::test]
async fn admin_scale_contract() {
    let st = state().await;
    let tok = mint(&st, "root", Scope::Admin).await;
    let app = app(st);
    let (status, body) = send_json(&app, "GET", "/admin/scale", bearer(&tok), None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body["fill"].is_u64());
    assert!(body["cap"].is_u64());
    assert!(body["queue_depth"].is_u64());
    assert!(body["warm_floor"].is_u64());
    assert!(body["last_decisions"].is_array());
}

#[tokio::test]
async fn admin_token_mint_contract() {
    let st = state().await;
    let admin_tok = mint(&st, "root", Scope::Admin).await;
    let app = app(st);
    let (status, body) = send_json(
        &app,
        "POST",
        "/admin/tokens",
        bearer(&admin_tok),
        Some(json!({"owner_id": "owner-x", "scope": "user"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body["token"].is_string() && !body["token"].as_str().unwrap().is_empty());
}

#[tokio::test]
async fn admin_token_mint_rejects_user_scope_token() {
    // A USER token must not mint (A6 — admin scope only on admin listener).
    let st = state().await;
    let user_tok = mint(&st, "owner-x", Scope::User).await;
    let app = app(st);
    let (status, _) = send_json(
        &app,
        "POST",
        "/admin/tokens",
        bearer(&user_tok),
        Some(json!({"owner_id": "owner-y", "scope": "user"})),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "user token on admin route"
    );
}

#[tokio::test]
async fn internal_register_contract() {
    let st = state().await;
    let tok = mint(&st, "pod-1", Scope::Pod).await;
    let app = app(st);
    let (status, body) = send_json(
        &app,
        "POST",
        "/internal/pods/register",
        bearer(&tok),
        Some(json!({"pod_id": "pod-1", "addr": "127.0.0.1:9001", "cap": 2, "versions": {"stages": ["mock"]}})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["registered"], true);
    assert!(body["assign_ws"].is_string());
}

#[tokio::test]
async fn internal_health_contract() {
    let st = state().await;
    let tok = mint(&st, "pod-h", Scope::Pod).await;
    let app = app(st);
    let (_, _) = send_json(
        &app,
        "POST",
        "/internal/pods/register",
        bearer(&tok),
        Some(json!({"pod_id": "pod-h", "addr": "127.0.0.1:9002", "cap": 2})),
    )
    .await;
    let (status, body) = send_json(
        &app,
        "POST",
        "/internal/pods/pod-h/health",
        bearer(&tok),
        Some(json!({"fill": 1})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["ok"], true);
}

// ---------------------------------------------------------------------------
// M4 — admin drain + static client serving.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn drain_pod_gets_no_new_assignments() {
    let st = state().await;
    let registry = st.registry.clone();
    let id = PodId("pod-drain".to_string());
    registry.register(&id, "127.0.0.1:0".to_string(), 2, None);
    registry.ready(&id);

    // Drain via the admin route.
    let admin_tok = mint(&st, "root", Scope::Admin).await;
    let app = app(st.clone());
    let (status, body) = send_json(
        &app,
        "POST",
        "/admin/pods/pod-drain/drain",
        bearer(&admin_tok),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["draining"], true);

    // A draining pod must not be picked for assignment (router filters by
    // Ready phase + live assign channel).
    let channels = orchestrator::PodAssignChannels::default();
    let pick = orchestrator::router::pick(&registry, u32::MAX, &channels);
    assert!(
        pick.is_none(),
        "draining pod must be excluded from assignment picks"
    );
}

#[tokio::test]
async fn drain_unknown_pod_404() {
    let st = state().await;
    let admin_tok = mint(&st, "root", Scope::Admin).await;
    let app = app(st);
    let (status, _) = send_json(
        &app,
        "POST",
        "/admin/pods/ghost/drain",
        bearer(&admin_tok),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn static_client_served_at_root() {
    let html = client_static::index_html(None);
    assert!(html.contains("VIHS"), "placeholder must mention VIHS");
    assert!(html.contains("</html>"));
}

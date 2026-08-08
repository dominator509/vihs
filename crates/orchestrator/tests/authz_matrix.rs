//! M2 authz matrix: every session-scoped route in the SPEC-003 registry is
//! exercised with owner / foreign / no-auth. The route list is PARSED from
//! the SPEC-003 markdown table so a new route added to the registry fails
//! this test until it is covered (SPEC-005 acceptance: fail closed).

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{header, HeaderMap, Request, StatusCode};
use axum::Router;
use orchestrator::authz::Scope;
use orchestrator::config::Config;
use orchestrator::memoryd_client::MemorydClient;
use orchestrator::{build_state, AppState};
use serde_json::{json, Value};
use tower::util::ServiceExt;

/// Path to the normative route registry (SPEC-003), relative to this crate.
const SPEC_003: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../.agent/specs/SPEC-003-api-contracts.md"
);

async fn state() -> Arc<AppState> {
    let mut cfg = Config::from_env();
    cfg.warm_pool_floor = 1;
    let mem = MemorydClient::new(&cfg.memoryd_addr);
    build_state(cfg, mem).await
}

fn app(st: Arc<AppState>) -> Router {
    Router::new()
        .merge(orchestrator::api_public::public_routes())
        .merge(orchestrator::api_admin::admin_routes())
        .merge(orchestrator::api_internal::internal_routes())
        .merge(orchestrator::signal_route::client_routes())
        .route("/healthz", axum::routing::get(|| async { "ok" }))
        .with_state(st)
}

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
    let bytes = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap_or_default();
    let body = serde_json::from_slice(&bytes).unwrap_or_else(|_| json!({}));
    (status, body)
}

/// Parse the SPEC-003 orchestrator public-API table into (method, path)
/// rows. Rows with `{id}` are session-scoped and belong in the matrix.
/// Only the section between "## Orchestrator public API" and the next "##"
/// header is parsed (the memoryd table lists `/v1/sessions/{id}/events`
/// etc. that are NOT orchestrator routes).
fn registry_session_routes() -> Vec<(String, String)> {
    let md = std::fs::read_to_string(SPEC_003)
        .unwrap_or_else(|e| panic!("cannot read SPEC-003 registry: {e}"));
    let mut in_public = false;
    let mut routes = Vec::new();
    for line in md.lines() {
        let line = line.trim();
        if line.starts_with("## ") {
            in_public = line.starts_with("## Orchestrator public API");
            continue;
        }
        if !in_public {
            continue;
        }
        if !line.starts_with('|') {
            continue;
        }
        let cols: Vec<&str> = line.split('|').map(|c| c.trim()).collect();
        if cols.len() < 3 {
            continue;
        }
        let cell = cols[1];
        // "POST /v1/sessions/{id}/resume" — method + path in the first col.
        let mut parts = cell.splitn(2, ' ');
        let (Some(method), Some(path)) = (parts.next(), parts.next()) else {
            continue;
        };
        let method = method.to_uppercase();
        let path = path.to_string();
        if !path.starts_with("/v1/sessions") {
            continue; // public session API only
        }
        if !path.contains("{id}") {
            continue; // create/list have no session scoping — separate test
        }
        routes.push((method, path));
    }
    routes
}

/// Create a session owned by `owner_tok`; returns its id.
async fn create_session(app: &Router, owner_tok: &str) -> String {
    let (status, body) = send_json(
        app,
        "POST",
        "/v1/sessions",
        bearer(owner_tok),
        Some(json!({"persona_id": "matrix"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "create: {body}");
    body["session_id"].as_str().expect("session_id").to_string()
}

#[tokio::test]
async fn authz_matrix_owner_foreign_none() {
    let st = state().await;
    let app = app(st.clone());
    let owner_tok = mint(&st, "matrix-owner", Scope::User).await;
    let foreign_tok = mint(&st, "matrix-foreign", Scope::User).await;

    let routes = registry_session_routes();
    assert!(
        !routes.is_empty(),
        "SPEC-003 registry parse returned no session routes — table format drift?"
    );

    for (method, path) in routes {
        // Fresh session per route — DELETE in a previous iteration must not
        // leak into later rows (transcript/resume need a live session).
        let sid = create_session(&app, &owner_tok).await;
        let uri = path.replace("{id}", &sid);

        // owner → authz passes (route-level outcome may be 2xx/503/409; the
        // point is it must NOT be 401 or 404).
        let (status, _) = send_json(&app, &method, &uri, bearer(&owner_tok), None).await;
        assert!(
            status != StatusCode::UNAUTHORIZED && status != StatusCode::NOT_FOUND,
            "{method} {uri} owner: expected authz pass, got {status}"
        );

        // foreign → 404, never 403 (no ID oracle, SPEC-005 A4).
        let (status, body) = send_json(&app, &method, &uri, bearer(&foreign_tok), None).await;
        assert_eq!(
            status,
            StatusCode::NOT_FOUND,
            "{method} {uri} foreign: expected 404, got {status} {body}"
        );

        // none → 401.
        let (status, body) = send_json(&app, &method, &uri, HeaderMap::new(), None).await;
        assert_eq!(
            status,
            StatusCode::UNAUTHORIZED,
            "{method} {uri} none: expected 401, got {status} {body}"
        );
    }
}

#[tokio::test]
async fn authz_matrix_create_and_list_owner_only() {
    let st = state().await;
    let app = app(st.clone());
    let owner_tok = mint(&st, "matrix-owner2", Scope::User).await;

    // create with no token → 401.
    let (status, _) = send_json(
        &app,
        "POST",
        "/v1/sessions",
        HeaderMap::new(),
        Some(json!({"persona_id": "matrix"})),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);

    // list with owner token → 200; with none → 401.
    let (status, body) = send_json(&app, "GET", "/v1/sessions", bearer(&owner_tok), None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, _) = send_json(&app, "GET", "/v1/sessions", HeaderMap::new(), None).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn authz_matrix_admin_listener_rejects_user_tokens() {
    let st = state().await;
    let app = app(st.clone());
    let user_tok = mint(&st, "matrix-user", Scope::User).await;

    for (method, path) in [
        ("GET", "/admin/pods"),
        ("GET", "/admin/scale"),
        ("POST", "/admin/pods/some-pod/drain"),
    ] {
        let (status, body) = send_json(&app, method, path, bearer(&user_tok), None).await;
        assert_eq!(
            status,
            StatusCode::UNAUTHORIZED,
            "{method} {path} user token: expected 401, got {status} {body}"
        );
    }
}

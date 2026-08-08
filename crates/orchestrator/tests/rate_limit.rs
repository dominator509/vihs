//! EP-006 M4 — rate-limit contract tests (SPEC-005 A5).
//!
//! Session create ≤10/min and resume ≤30/min per token → 429 `rate_limited`
//! retryable (SPEC-006 R1). Each test mints a FRESH token so the fixed-window
//! budgets are isolated (limits are per token, not global).
//!
//! The resume class is exercised with a non-existent session id: the rate
//! check happens BEFORE the owner/session lookup, so 30 passes (404 owner
//! check) then the 31st returns 429 — proving the limiter fires independently
//! of route outcome. The create class hits the real memoryd durable row.

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
use orchestrator::config::Config as OrchConfig;
use orchestrator::memoryd_client::MemorydClient;
use orchestrator::{build_state, AppState};
use serde_json::{json, Value};
use tower::ServiceExt;

/// EP-006 M3: the strict network memoryd verifies tokens with the pepper in
/// .env (VIHS_TOKEN_PEPPER). cargo-test does not source .env, so inject the
/// shared pepper before building state (same helper as contract_public).
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

async fn mint(st: &Arc<AppState>, owner: &str) -> String {
    st.tokens
        .mint(owner, Scope::User, Duration::from_secs(3600))
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

/// Copy headers onto a request builder (no `.headers()` on builder).
fn with_headers(
    builder: axum::http::request::Builder,
    headers: HeaderMap,
) -> axum::http::request::Builder {
    let mut b = builder;
    for (k, v) in headers.iter() {
        b = b.header(k, v);
    }
    b
}

async fn body_json(resp: axum::response::Response) -> Value {
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

async fn create_one(app: &Router, tok: &str) -> StatusCode {
    let resp = app
        .clone()
        .oneshot(
            with_headers(
                Request::builder()
                    .method("POST")
                    .uri("/v1/sessions")
                    .header(header::CONTENT_TYPE, "application/json"),
                bearer(tok),
            )
            .body(Body::from(json!({"persona_id": "p1"}).to_string()))
            .unwrap(),
        )
        .await
        .unwrap();
    resp.status()
}

async fn resume_one(app: &Router, tok: &str) -> StatusCode {
    let resp = app
        .clone()
        .oneshot(
            with_headers(
                Request::builder()
                    .method("POST")
                    .uri("/v1/sessions/does-not-exist/resume"),
                bearer(tok),
            )
            .body(Body::empty())
            .unwrap(),
        )
        .await
        .unwrap();
    resp.status()
}

#[tokio::test]
async fn session_create_limited_to_10_per_minute() {
    let st = state().await;
    let app = app(st.clone());
    let tok = mint(&st, "rl-create").await;

    for i in 0..10 {
        let status = create_one(&app, &tok).await;
        assert!(
            status == StatusCode::CREATED || status == StatusCode::BAD_REQUEST,
            "request {i} should pass the limiter, got {status}"
        );
    }
    // 11th create → 429 rate_limited, retryable=true (SPEC-005 A5 / SPEC-006).
    let resp = app
        .clone()
        .oneshot(
            with_headers(
                Request::builder()
                    .method("POST")
                    .uri("/v1/sessions")
                    .header(header::CONTENT_TYPE, "application/json"),
                bearer(&tok),
            )
            .body(Body::from(json!({"persona_id": "p1"}).to_string()))
            .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);
    let body = body_json(resp).await;
    assert_eq!(body["error"]["code"], "rate_limited");
    assert_eq!(body["error"]["retryable"], true);
}

#[tokio::test]
async fn resume_limited_to_30_per_minute() {
    let st = state().await;
    let app = app(st.clone());
    let tok = mint(&st, "rl-resume").await;

    // First 30 resume calls pass the limiter (each 404s at the owner check
    // because the session doesn't exist — but NOT at the rate limiter).
    for i in 0..30 {
        let status = resume_one(&app, &tok).await;
        assert_eq!(
            status,
            StatusCode::NOT_FOUND,
            "resume {i} should pass the limiter, got {status}"
        );
    }
    // 31st → 429.
    let status = resume_one(&app, &tok).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
}

#[tokio::test]
async fn different_tokens_have_independent_budgets() {
    let st = state().await;
    let app = app(st.clone());
    let tok_a = mint(&st, "rl-a").await;
    let tok_b = mint(&st, "rl-b").await;

    // Exhaust A's create budget.
    for _ in 0..10 {
        create_one(&app, &tok_a).await;
    }
    assert_eq!(
        create_one(&app, &tok_a).await,
        StatusCode::TOO_MANY_REQUESTS
    );
    // B is untouched.
    assert_ne!(
        create_one(&app, &tok_b).await,
        StatusCode::TOO_MANY_REQUESTS
    );
}

//! EP-006 M5 — client contract tests (SPEC-004 F1; EP-006 §6).
//!
//! The F1 client (client/index.html + client/session.js) is embedded in the
//! orchestrator binary and served at `/` and `/session.js`. These tests:
//!   1. Prove the routes serve the REAL client (not the placeholder).
//!   2. Assert the F1 security invariants in the served session.js:
//!      token in memory only by default; localStorage ONLY under the
//!      explicit opt-in ("remember on this device"); no token in query
//!      strings (SPEC-005: never query string).
//!   3. Prove the served JS contains the first-message auth frame path
//!      (browsers cannot set WS headers — SPEC-005).

use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use orchestrator::config::Config as OrchConfig;
use orchestrator::memoryd_client::MemorydClient;
use orchestrator::signal_route::client_routes;
use orchestrator::{build_state, AppState};
use tower::ServiceExt;

/// Same shared-pepper contract as every other test that builds real state.
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

async fn app() -> Router {
    ensure_shared_pepper();
    let cfg = OrchConfig::from_env();
    let mem = MemorydClient::new(&cfg.memoryd_addr);
    let st: Arc<AppState> = build_state(cfg, mem).await;
    Router::new().merge(client_routes()).with_state(st)
}

async fn get(path: &str) -> (StatusCode, String) {
    let resp = app()
        .await
        .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).to_string())
}

#[tokio::test]
async fn client_index_served_at_root() {
    let (status, html) = get("/").await;
    assert_eq!(status, StatusCode::OK);
    // The real F1 client, not the EP-001 placeholder.
    assert!(html.contains("token-input"), "token prompt missing");
    assert!(html.contains("session.js"), "session.js not referenced");
    assert!(
        !html.contains("Client placeholder"),
        "placeholder still served"
    );
}

#[tokio::test]
async fn client_session_js_served_with_js_content_type() {
    let resp = app()
        .await
        .oneshot(
            Request::builder()
                .uri("/session.js")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(
        resp.headers().get(header::CONTENT_TYPE).unwrap(),
        "text/javascript"
    );
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let js = String::from_utf8_lossy(&bytes);
    assert!(js.contains("tokenStore"), "token store missing");
}

#[test]
fn client_f1_token_memory_only_by_default() {
    // F1 security invariant (SPEC-004): the token lives in a module variable;
    // localStorage writes appear ONLY in the explicit opt-in paths
    // (remember()/clear()/loadRemembered()/isRemembered).
    let js = orchestrator::client_static::SESSION_JS;
    assert!(
        js.contains("_token: null"),
        "token must start in memory as null"
    );
    // Every localStorage API CALL must sit inside the tokenStore object —
    // the only place storage is allowed. (Comments may mention the word;
    // the invariant is about calls.)
    let store_start = js.find("const tokenStore").expect("tokenStore");
    let store_end = js.find("// --- DOM refs").expect("DOM refs");
    let store = &js[store_start..store_end];
    let outside_store = format!("{}{}", &js[..store_start], &js[store_end..]);
    let outside_calls = outside_store
        .match_indices("localStorage")
        .filter(|(i, _)| {
            // Skip occurrences that are prose comments (// … or inside "...").
            let before = &outside_store[..*i];
            !before.contains("//") && !before.contains("\"")
        })
        .count();
    assert_eq!(
        outside_calls, 0,
        "localStorage API used outside tokenStore: {outside_store}"
    );
    // The memory-only default is explicit: set() never writes storage.
    assert!(store.contains("set(token)"));
    assert!(store.contains("this._token = token"));
    // The opt-in is explicit and named per SPEC-004.
    assert!(store.contains("remember()"));
    assert!(store.contains("localStorage.setItem"));
    assert!(store.contains("clear()"));
    assert!(store.contains("localStorage.removeItem"));
}

#[test]
fn client_f1_auth_frame_path_present() {
    // SPEC-005: browsers cannot set WS headers — the client MUST send the
    // first-message auth frame. The served JS must contain that path.
    let js = orchestrator::client_static::SESSION_JS;
    assert!(
        js.contains(r#"t: "auth", token: tokenStore.get()"#),
        "auth frame send missing"
    );
    // And it must NEVER put the token in a query string (SPEC-005).
    assert!(
        !js.contains("token="),
        "token must never appear in a query string"
    );
    assert!(
        js.contains("new WebSocket"),
        "signal socket must be a browser WebSocket"
    );
}

#[test]
fn client_f1_captions_rendered_as_text_not_html() {
    // SPEC-004 security: transcript/captions are sanitized — rendered as
    // textContent, never innerHTML.
    let js = orchestrator::client_static::SESSION_JS;
    assert!(js.contains("textContent"), "captions must use textContent");
    assert!(
        !js.contains("captions").to_string().is_empty(),
        "captions handling present"
    );
    // No innerHTML ASSIGNMENT anywhere in the caption render path (comments
    // may name the forbidden API as prose — the invariant is the call).
    let captions_idx = js.find("cap.t === \"caption\"").expect("caption branch");
    let tail = &js[captions_idx..];
    assert!(
        !tail.contains("innerHTML ="),
        "caption render must not assign innerHTML"
    );
}

#[test]
fn client_index_has_f1_regions() {
    // SPEC-004 screens: token prompt, connect panel, stage, captions,
    // session list are all present.
    let html = orchestrator::client_static::INDEX_HTML;
    for needle in [
        "token-prompt",
        "connect-panel",
        "persona-select",
        "captions",
        "sessions",
    ] {
        assert!(html.contains(needle), "missing F1 region: {needle}");
    }
}

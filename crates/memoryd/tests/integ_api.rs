//! Contract tests for the memoryd API surface (EP-003 M7; SPEC-003 memoryd
//! table). Every route row has a request/response schema assertion.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use http_body_util::BodyExt;
use memoryd::api::{dev_state, router};
use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::store::S3Store;
use memoryd::writer::WriterRegistry;
use serde_json::json;
use tower::ServiceExt;
use vihs_core::ids::SessionId;

fn test_config() -> Config {
    std::env::set_var(
        "VIHS_REDIS_URL",
        std::env::var("VIHS_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379".into()),
    );
    std::env::set_var(
        "VIHS_S3_ENDPOINT",
        std::env::var("VIHS_S3_ENDPOINT").unwrap_or_else(|_| "http://127.0.0.1:9000".into()),
    );
    std::env::set_var(
        "VIHS_S3_BUCKET",
        std::env::var("VIHS_S3_BUCKET").unwrap_or_else(|_| "vihs-sessions".into()),
    );
    std::env::set_var(
        "VIHS_S3_ACCESS_KEY",
        std::env::var("VIHS_S3_ACCESS_KEY").unwrap_or_else(|_| "minioadmin".into()),
    );
    std::env::set_var(
        "VIHS_S3_SECRET_KEY",
        std::env::var("VIHS_S3_SECRET_KEY").unwrap_or_else(|_| "minioadmin".into()),
    );
    std::env::set_var(
        "VIHS_TOKEN_PEPPER",
        std::env::var("VIHS_TOKEN_PEPPER").unwrap_or_else(|_| "test-memoryd-pepper".into()),
    );
    Config::from_env()
}

async fn app() -> axum::Router {
    let cfg = test_config();
    let store = Arc::new(S3Store::new(&cfg).await);
    let index = Arc::new(RedisIndex::new(&cfg.redis_url).await.unwrap());
    let registry = WriterRegistry::new(store.clone(), index.clone());
    let state = dev_state(cfg, store, index, registry);
    router(state)
}

async fn body_json(resp: axum::response::Response) -> serde_json::Value {
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null)
}

#[tokio::test]
async fn healthz_ok() {
    let resp = app()
        .await
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn append_then_transcript_contract() {
    let app = app().await;
    let sid = SessionId::new();

    // Create session via index directly (orchestrator owns this row in prod;
    // memoryd reads it).
    let cfg = test_config();
    let store = Arc::new(S3Store::new(&cfg).await);
    let index = Arc::new(RedisIndex::new(&cfg.redis_url).await.unwrap());
    index
        .create_session(&sid, "owner", "2026-07-05T09:00:00Z")
        .await
        .unwrap();

    // POST /v1/sessions/{sid}/events — append row.
    let body = json!({
        "v": 1, "session_id": sid.as_str(), "turn_id": 1,
        "ts": "2026-07-05T09:01:00.000Z", "role": "user", "kind": "utterance",
        "text": "hello contract", "meta": {"asr_conf_bp": 9400, "interrupted": false}
    });
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/sessions/{sid}/events"))
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::AUTHORIZATION, "Bearer dev-token")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = body_json(resp).await;
    assert_eq!(v["status"], "committed");
    assert!(v["hash"].as_str().unwrap().starts_with("blake3:"));
    assert_eq!(v["turn_id"], 1);

    // GET /v1/sessions/{sid}/transcript — render row (404 vs markdown).
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/v1/sessions/{sid}/transcript"))
                .header(header::AUTHORIZATION, "Bearer dev-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let text = String::from_utf8(bytes.to_vec()).unwrap();
    assert!(
        text.contains("hello contract"),
        "transcript contains the turn"
    );

    // POST /v1/sessions/{sid}/load — cursor row.
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/sessions/{sid}/load"))
                .header(header::AUTHORIZATION, "Bearer dev-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = body_json(resp).await;
    assert!(v["cursor"]["tip_hash"]
        .as_str()
        .unwrap()
        .starts_with("blake3:"));
    assert_eq!(v["cursor"]["last_turn_id"], 1);

    // DELETE /v1/sessions/{sid} — 204.
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!("/v1/sessions/{sid}"))
                .header(header::AUTHORIZATION, "Bearer dev-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);

    // Foreign/unknown session → 404, never 403 (SPEC-005 no-ID-oracle).
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/sessions/unknown-session/transcript")
                .header(header::AUTHORIZATION, "Bearer dev-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::NOT_FOUND,
        "unknown session → 404"
    );
    let _ = store;
}

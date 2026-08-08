//! EP-006 M3 — memoryd authz: pod token bound to one session + allowed
//! verbs (SPEC-005 A1/A3/A7); signed-URL TTL test (A3). Runs against the
//! strict TokenAuthorizer (NOT dev_state) over the real router, requiring
//! dev services (Redis + MinIO — scripts/dev-services.sh).
//!
//! Required tests per SPEC-005: pod-token boundary (second session 404;
//! forbidden verb 403); signed-URL expiry test; owner-match negatives.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use http_body_util::BodyExt;
use memoryd::api::{router, ApiState};
use memoryd::authz::{TokenAuthorizer, Verb};
use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::store::S3Store;
use memoryd::writer::WriterRegistry;
use serde_json::{json, Value};
use tower::ServiceExt;
use vihs_auth::{Scope, TokenStore, DEFAULT_TOKEN_TTL, POD_TOKEN_TTL};
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

/// Build the router with the STRICT TokenAuthorizer (the seam under test).
async fn authed_app() -> (axum::Router, TokenStore) {
    let cfg = test_config();
    let store = Arc::new(S3Store::new(&cfg).await);
    let index = Arc::new(RedisIndex::new(&cfg.redis_url).await.unwrap());
    let registry = WriterRegistry::new(store.clone(), index.clone());
    let tokens = TokenStore::connect(&cfg.redis_url, cfg.token_pepper.clone())
        .await
        .expect("token store connect");
    let authz = TokenAuthorizer::new(tokens.clone(), index.clone());
    let state = Arc::new(ApiState {
        cfg,
        store,
        index,
        registry,
        authz: Box::new(authz),
    });
    (router(state), tokens)
}

async fn body_json(resp: axum::response::Response) -> Value {
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

fn event_body(sid: &SessionId, turn: u64, text: &str) -> Value {
    json!({
        "v": 1,
        "session_id": sid.as_str(),
        "turn_id": turn,
        "ts": "2026-07-07T18:22:31.482Z",
        "role": "user",
        "kind": "utterance",
        "text": text,
        "meta": {"asr_conf_bp": 9400, "interrupted": false}
    })
}

async fn append(app: &axum::Router, sid: &SessionId, token: &str, turn: u64) -> StatusCode {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/sessions/{sid}/events"))
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::AUTHORIZATION, format!("Bearer {token}"))
                .body(Body::from(event_body(sid, turn, "hello").to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    resp.status()
}

async fn load(app: &axum::Router, sid: &SessionId, token: &str) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/sessions/{sid}/load"))
                .header(header::AUTHORIZATION, format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let v = body_json(resp).await;
    (status, v)
}

async fn delete(app: &axum::Router, sid: &SessionId, token: &str) -> StatusCode {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!("/v1/sessions/{sid}"))
                .header(header::AUTHORIZATION, format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    resp.status()
}

/// The create path: a user token opens a session that has no index record
/// yet (the orchestrator's session-create appends a system note); memoryd
/// registers it with the principal's owner (SPEC-005 A1 stamping).
#[tokio::test]
async fn authz_user_token_create_append_load_ok() {
    let (app, tokens) = authed_app().await;
    let sid = SessionId::new();
    let user = tokens
        .mint("alice", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();

    // First append on a brand-new session: allowed + owner stamped.
    assert_eq!(append(&app, &sid, &user, 1).await, StatusCode::OK);

    // Load with the same user token: owner match.
    let (status, v) = load(&app, &sid, &user).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["cursor"]["last_turn_id"], 1);
}

/// Signed-URL TTL (SPEC-005 A3: signed memory URL with the pod TTL — 900s).
#[tokio::test]
async fn authz_signed_url_expires_in_900_seconds() {
    let (app, tokens) = authed_app().await;
    let sid = SessionId::new();
    let user = tokens
        .mint("alice", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();
    assert_eq!(append(&app, &sid, &user, 1).await, StatusCode::OK);

    let (status, v) = load(&app, &sid, &user).await;
    assert_eq!(status, StatusCode::OK);
    let url = v["memory_url"].as_str().expect("memory_url present");
    assert!(
        url.contains("X-Amz-Expires=900"),
        "signed URL must carry a 900s TTL, got: {url}"
    );
}

/// Pod token bound to ITS session: append + load allowed.
#[tokio::test]
async fn authz_pod_token_bound_session_append_load_ok() {
    let (app, tokens) = authed_app().await;
    let sid = SessionId::new();
    let user = tokens
        .mint("alice", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();
    assert_eq!(append(&app, &sid, &user, 1).await, StatusCode::OK);

    let pod = tokens
        .mint(sid.as_str(), Scope::Pod, POD_TOKEN_TTL)
        .await
        .unwrap();
    assert_eq!(append(&app, &sid, &pod, 2).await, StatusCode::OK);
    let (status, _) = load(&app, &sid, &pod).await;
    assert_eq!(status, StatusCode::OK);
}

/// SPEC-005 required negative: pod token on a SECOND session → 404 (the
/// bound session_id != path id; no ID oracle).
#[tokio::test]
async fn authz_pod_token_second_session_404() {
    let (app, tokens) = authed_app().await;
    let sid_a = SessionId::new();
    let sid_b = SessionId::new();
    let user = tokens
        .mint("alice", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();
    assert_eq!(append(&app, &sid_a, &user, 1).await, StatusCode::OK);
    assert_eq!(append(&app, &sid_b, &user, 1).await, StatusCode::OK);

    let pod_a = tokens
        .mint(sid_a.as_str(), Scope::Pod, POD_TOKEN_TTL)
        .await
        .unwrap();

    // Append with pod_a's token to sid_b → 404, never 403.
    let status = append(&app, &sid_b, &pod_a, 2).await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "pod token must not cross sessions"
    );

    // Load sid_b with pod_a's token → 404 too.
    let (status, _) = load(&app, &sid_b, &pod_a).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

/// SPEC-005 required negative: pod token on a FORBIDDEN verb (delete) → 403.
#[tokio::test]
async fn authz_pod_token_forbidden_verb_403() {
    let (app, tokens) = authed_app().await;
    let sid = SessionId::new();
    let user = tokens
        .mint("alice", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();
    assert_eq!(append(&app, &sid, &user, 1).await, StatusCode::OK);

    let pod = tokens
        .mint(sid.as_str(), Scope::Pod, POD_TOKEN_TTL)
        .await
        .unwrap();
    let status = delete(&app, &sid, &pod).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "pods never delete (A1)");
}

/// Foreign-owner user token → 404 (no ID oracle, A4).
#[tokio::test]
async fn authz_user_token_foreign_owner_404() {
    let (app, tokens) = authed_app().await;
    let sid = SessionId::new();
    let alice = tokens
        .mint("alice", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();
    let bob = tokens
        .mint("bob", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();
    assert_eq!(append(&app, &sid, &alice, 1).await, StatusCode::OK);

    let (status, _) = load(&app, &sid, &bob).await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "bob must not see alice's session"
    );
    let status = delete(&app, &sid, &bob).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

/// Owner hard-delete (A7): the owner can delete; verified by a follow-up
/// load returning 404.
#[tokio::test]
async fn authz_owner_delete_ok_and_session_gone() {
    let (app, tokens) = authed_app().await;
    let sid = SessionId::new();
    let alice = tokens
        .mint("alice", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();
    assert_eq!(append(&app, &sid, &alice, 1).await, StatusCode::OK);

    assert_eq!(delete(&app, &sid, &alice).await, StatusCode::NO_CONTENT);
    let (status, _) = load(&app, &sid, &alice).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "session must be gone");
}

/// No bearer → 401. Garbage bearer → 401.
#[tokio::test]
async fn authz_missing_and_invalid_token_401() {
    let (app, tokens) = authed_app().await;
    let sid = SessionId::new();
    let user = tokens
        .mint("alice", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();
    assert_eq!(append(&app, &sid, &user, 1).await, StatusCode::OK);

    // No header at all.
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/sessions/{sid}/load"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);

    // Garbage (non-32-byte) bearer.
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/v1/sessions/{sid}/load"))
                .header(header::AUTHORIZATION, "Bearer garbage")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
}

/// Heal-only (owner-less) records — created by rebuild/recovery replaying
/// the object store (heal does not touch owner) — must be bindable by the
/// create path: a valid user token appending claims them (EP-006 M3 Decision
/// Log). Without this, rebuilt sessions are permanently inaccessible.
#[tokio::test]
async fn authz_ownerless_record_binds_on_append() {
    let (app, tokens) = authed_app().await;
    let sid = SessionId::new();
    let alice = tokens
        .mint("alice", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();
    let bob = tokens
        .mint("bob", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .unwrap();

    // Simulate a heal-only record (no owner field) — the rebuild fingerprint.
    let index = RedisIndex::new("redis://127.0.0.1:6379").await.unwrap();
    index
        .heal(
            &sid,
            "blake3:0000000000000000000000000000000000000000000000000000000000000000",
            0,
            0,
            0,
            None,
        )
        .await
        .unwrap();

    // Alice's append binds the owner (create path).
    assert_eq!(append(&app, &sid, &alice, 1).await, StatusCode::OK);
    let (status, _) = load(&app, &sid, &alice).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "owner must be bound after create-path append"
    );

    // Bob still can't see it (404, no oracle).
    let (status, _) = load(&app, &sid, &bob).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

/// Verb enumeration sanity: the Authorizer trait still exposes the four
/// memoryd verbs (compile-time contract used by api.rs call sites).
#[test]
fn authz_verb_set_is_stable() {
    let verbs = [Verb::Append, Verb::Load, Verb::Delete, Verb::Admin];
    assert_eq!(verbs.len(), 4);
}

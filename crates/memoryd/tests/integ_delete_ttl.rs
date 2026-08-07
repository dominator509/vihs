//! Integration: hard delete + TTL sweep (EP-003 M6; SPEC-002 D-9/D-10).

use std::sync::Arc;

use chrono::{Duration, Utc};
use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::store::{ObjectStore, S3Store};
use memoryd::sweep::{hard_delete, ttl_sweep};
use memoryd::writer::WriterRegistry;
use serde_json::json;
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
    Config::from_env()
}

fn utt(sid: &SessionId, turn: u64, text: &str) -> serde_json::Value {
    json!({
        "v": 1, "session_id": sid.as_str(), "turn_id": turn,
        "ts": "2026-07-05T09:01:00.000Z", "role": "user", "kind": "utterance",
        "text": text, "meta": {"asr_conf_bp": 9400, "interrupted": false}
    })
}

#[tokio::test]
async fn hard_delete_removes_everything_and_is_idempotent() {
    let cfg = test_config();
    let store = Arc::new(S3Store::new(&cfg).await);
    let index = Arc::new(RedisIndex::new(&cfg.redis_url).await.unwrap());
    let registry = WriterRegistry::new(store.clone(), index.clone());
    let sid = SessionId::new();
    index
        .create_session(&sid, "test-owner", "2026-07-05T09:00:00Z")
        .await
        .unwrap();
    let handle = registry.get(&sid);
    handle.append(utt(&sid, 1, "hello")).await.unwrap();
    store
        .put_artifact(&sid, "transcript.md", b"# x")
        .await
        .unwrap();

    let deleted = hard_delete(&sid, &store, &index, Some("test-owner"))
        .await
        .unwrap();
    assert!(deleted >= 2, "events + artifact removed: {deleted}");

    // Idempotent: re-delete is a no-op OK.
    let again = hard_delete(&sid, &store, &index, Some("test-owner"))
        .await
        .unwrap();
    assert_eq!(again, 0);

    // Zero keys/objects remain.
    assert!(index.snapshot(&sid).await.is_err(), "index gone");
    assert_eq!(store.read_log(&sid).await.unwrap().len(), 0, "log gone");
    assert!(
        store.get_artifact(&sid, "transcript.md").await.is_err(),
        "artifact gone"
    );
}

#[tokio::test]
async fn ttl_sweep_deletes_only_expired_with_injected_clock() {
    let cfg = test_config();
    let store = Arc::new(S3Store::new(&cfg).await);
    let index = Arc::new(RedisIndex::new(&cfg.redis_url).await.unwrap());
    let registry = WriterRegistry::new(store.clone(), index.clone());

    let fresh = SessionId::new();
    index
        .create_session(&fresh, "test-owner", "2026-07-05T09:00:00Z")
        .await
        .unwrap();
    registry
        .get(&fresh)
        .append(utt(&fresh, 1, "fresh"))
        .await
        .unwrap();

    let stale = SessionId::new();
    index
        .create_session(&stale, "test-owner", "2026-01-01T09:00:00Z")
        .await
        .unwrap();
    registry
        .get(&stale)
        .append(utt(&stale, 1, "stale"))
        .await
        .unwrap();
    // Backdate the stale session's updated_at past the TTL.
    let client = redis::Client::open(cfg.redis_url.clone()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let _: () = redis::cmd("HSET")
        .arg(format!("session:{stale}"))
        .arg("updated_at")
        .arg((Utc::now() - Duration::days(400)).to_rfc3339())
        .query_async(&mut conn)
        .await
        .unwrap();

    let deleted = ttl_sweep(&store, &index, 90, Utc::now()).await.unwrap();
    assert!(deleted.contains(&stale), "stale deleted: {deleted:?}");
    assert!(!deleted.contains(&fresh), "fresh kept");

    assert!(index.snapshot(&stale).await.is_err());
    assert!(index.snapshot(&fresh).await.is_ok());

    memoryd::sweep::hard_delete(&fresh, &store, &index, Some("test-owner"))
        .await
        .unwrap();
}

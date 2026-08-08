//! Integration: load / render / artifacts / signed URLs (EP-003 M3).

use std::sync::Arc;
use std::time::Duration;

use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::store::{ObjectStore, S3Store, ARTIFACT_MEMORY};
use memoryd::writer::WriterRegistry;
use serde_json::json;
use vihs_core::chain::fsck;
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

fn sys_event(sid: &SessionId) -> serde_json::Value {
    json!({
        "v": 1, "session_id": sid.as_str(), "turn_id": 0,
        "ts": "2026-07-05T09:00:00.000Z", "role": "system", "kind": "note",
        "text": "Aria", "meta": {"interrupted": false}
    })
}

fn utt(sid: &SessionId, turn: u64, role: &str, text: &str) -> serde_json::Value {
    json!({
        "v": 1, "session_id": sid.as_str(), "turn_id": turn,
        "ts": "2026-07-05T09:01:00.000Z", "role": role, "kind": "utterance",
        "text": text, "meta": {"asr_conf_bp": 9400, "interrupted": false, "latency_ms": 812}
    })
}

#[tokio::test]
async fn load_returns_cursor_and_signed_url() {
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
    handle.append(sys_event(&sid)).await.unwrap();
    handle.append(utt(&sid, 1, "user", "Hello!")).await.unwrap();
    handle
        .append(utt(&sid, 1, "assistant", "Hi there!"))
        .await
        .unwrap();

    // Load path: snapshot + fsck + cursor fields + signed memory URL.
    let snap = index.snapshot(&sid).await.unwrap();
    let bytes = store.read_log(&sid).await.unwrap();
    let values: Vec<serde_json::Value> = bytes
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_slice(l).ok())
        .collect();
    let (_n, _tip) = fsck(values.iter()).unwrap();
    assert_eq!(snap.last_turn_id, Some(1));

    let memory_url = store
        .sign_get(
            &format!("sessions/{sid}/{ARTIFACT_MEMORY}"),
            Duration::from_secs(900),
        )
        .await
        .unwrap();
    let url = memory_url.to_string();
    assert!(
        url.starts_with("http://127.0.0.1:9000/"),
        "signed URL against MinIO: {url}"
    );
    assert!(
        url.contains("X-Amz-Expires=900") || url.contains("X-Amz-Signature"),
        "presigned params: {url}"
    );

    // Sign-put URL also works (audio upload path).
    let put_url = store
        .sign_put(
            &format!("sessions/{sid}/turn-1.opus"),
            Duration::from_secs(900),
        )
        .await
        .unwrap();
    assert!(put_url.to_string().starts_with("http://127.0.0.1:9000/"));

    memoryd::sweep::hard_delete(&sid, &store, &index, Some("test-owner"))
        .await
        .unwrap();
}

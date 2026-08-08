//! Integration: append → read_log round-trip, idempotent duplicate,
//! fsck of concatenation (EP-003 M1). Requires dev services (Redis+MinIO).

use std::sync::Arc;

use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::store::{ObjectStore, S3Store};
use memoryd::writer::WriterRegistry;
use serde_json::json;
use vihs_core::chain::fsck;
use vihs_core::ids::SessionId;

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn test_config() -> Config {
    // Allow overriding via env so the suite can point at dev services.
    std::env::set_var(
        "VIHS_REDIS_URL",
        env_or("VIHS_REDIS_URL", "redis://127.0.0.1:6379"),
    );
    std::env::set_var(
        "VIHS_S3_ENDPOINT",
        env_or("VIHS_S3_ENDPOINT", "http://127.0.0.1:9000"),
    );
    std::env::set_var("VIHS_S3_BUCKET", env_or("VIHS_S3_BUCKET", "vihs-sessions"));
    std::env::set_var(
        "VIHS_S3_ACCESS_KEY",
        env_or("VIHS_S3_ACCESS_KEY", "minioadmin"),
    );
    std::env::set_var(
        "VIHS_S3_SECRET_KEY",
        env_or("VIHS_S3_SECRET_KEY", "minioadmin"),
    );
    std::env::set_var(
        "VIHS_TOKEN_PEPPER",
        std::env::var("VIHS_TOKEN_PEPPER").unwrap_or_else(|_| "test-memoryd-pepper".into()),
    );
    Config::from_env()
}

fn event_body(sid: &SessionId, turn: u64, text: &str) -> serde_json::Value {
    json!({
        "v": 1,
        "session_id": sid.as_str(),
        "turn_id": turn,
        "ts": "2026-07-07T18:22:31.482Z",
        "role": "user",
        "kind": "utterance",
        "text": text,
        "meta": {"asr_conf_bp": 9400, "interrupted": false, "latency_ms": 812}
    })
}

#[tokio::test]
async fn append_roundtrip_and_duplicate() {
    let cfg = test_config();
    let store = Arc::new(S3Store::new(&cfg).await);
    let index = Arc::new(RedisIndex::new(&cfg.redis_url).await.unwrap());
    let registry = WriterRegistry::new(store.clone(), index.clone());
    let sid = SessionId::new();

    index
        .create_session(&sid, "test-owner", "2026-07-07T18:22:00Z")
        .await
        .unwrap();
    let handle = registry.get(&sid);

    // Append 3 events; verify committed hashes chain from genesis.
    let mut last_hash: Option<String> = None;
    for (i, text) in ["hello", "second", "third"].iter().enumerate() {
        let body = event_body(&sid, (i + 1) as u64, text);
        let res = handle.append(body).await.unwrap();
        match res {
            memoryd::writer::AppendResult::Committed { hash, turn_id } => {
                assert_eq!(turn_id, (i + 1) as u64);
                last_hash = Some(hash);
            }
            other => panic!("expected committed, got {other:?}"),
        }
    }
    assert!(last_hash.is_some());

    // Idempotent duplicate (D-5): retry the SAME body back-to-back — the
    // pod resends after a network drop before the reply arrived. Same prev →
    // same hash → Duplicate acked, no second line written.
    let body4 = event_body(&sid, 4, "fourth");
    match handle.append(body4.clone()).await.unwrap() {
        memoryd::writer::AppendResult::Committed { .. } => {}
        other => panic!("expected committed, got {other:?}"),
    }
    let dup = handle.append(body4).await.unwrap();
    assert!(matches!(
        dup,
        memoryd::writer::AppendResult::Duplicate { .. }
    ));

    // read_log concatenation passes fsck (INV-2 evidence).
    let bytes = store.read_log(&sid).await.unwrap();
    let values: Vec<serde_json::Value> = bytes
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_slice(l).ok())
        .collect();
    let (n, _tip) = fsck(values.iter()).unwrap();
    assert_eq!(n, 4, "four committed events, no duplicate line");

    // Cleanup (D-9 path).
    memoryd::sweep::hard_delete(&sid, &store, &index, Some("test-owner"))
        .await
        .unwrap();
    assert_eq!(store.read_log(&sid).await.unwrap().len(), 0);
}

//! Integration: crash-order recovery (EP-003 M2).
//!
//! Simulates a kill between store-append and index-advance: writes a sealed
//! event line directly to the store WITHOUT advancing Redis (the exact
//! crash window), then starts a fresh writer and asserts the next append
//! continues the chain — no duplication, no tear.

use std::sync::Arc;

use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::store::{ObjectStore, S3Store};
use memoryd::writer::WriterRegistry;
use serde_json::json;
use vihs_core::chain::{compute_hash, fsck, seal, GENESIS};
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

fn body(sid: &SessionId, turn: u64, text: &str) -> serde_json::Value {
    json!({
        "v": 1, "session_id": sid.as_str(), "turn_id": turn,
        "ts": "2026-07-07T18:22:31.482Z", "role": "user", "kind": "utterance",
        "text": text, "meta": {"asr_conf_bp": 9400, "interrupted": false}
    })
}

#[tokio::test]
async fn crash_between_store_and_index_heals() {
    let cfg = test_config();
    let store = Arc::new(S3Store::new(&cfg).await);
    let index = Arc::new(RedisIndex::new(&cfg.redis_url).await.unwrap());
    let sid = SessionId::new();
    index
        .create_session(&sid, "test-owner", "2026-07-07T18:22:00Z")
        .await
        .unwrap();

    // Normal append #1 through the writer (index advanced).
    let registry = WriterRegistry::new(store.clone(), index.clone());
    let h1 = match registry
        .get(&sid)
        .append(body(&sid, 1, "first"))
        .await
        .unwrap()
    {
        memoryd::writer::AppendResult::Committed { hash, .. } => hash,
        other => panic!("expected committed, got {other:?}"),
    };

    // CRASH WINDOW: event #2 reaches the store but Redis is never advanced.
    let mut raw2 = body(&sid, 2, "second");
    raw2["prev_hash"] = json!(h1.clone());
    let sealed2 = seal(raw2, &h1).unwrap();
    let _h2 = compute_hash(&sealed2).unwrap();
    store.append_line(&sid, &sealed2).await.unwrap();
    // Index still says tip = h1 (event #2 is invisible to Redis).
    let snap = index.snapshot(&sid).await.unwrap();
    assert_eq!(snap.tip_hash.as_deref(), Some(h1.as_str()));

    // RESTART: a fresh registry (new writer task) must recover the tip from
    // the log and heal, so append #3 continues the chain from h2.
    let registry2 = WriterRegistry::new(store.clone(), index.clone());
    let h3 = match registry2
        .get(&sid)
        .append(body(&sid, 3, "third"))
        .await
        .unwrap()
    {
        memoryd::writer::AppendResult::Committed { hash, .. } => hash,
        other => panic!("expected committed, got {other:?}"),
    };

    // Whole log fsck passes: 3 events, chain intact, no tear, no duplication.
    let bytes = store.read_log(&sid).await.unwrap();
    let values: Vec<serde_json::Value> = bytes
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_slice(l).ok())
        .collect();
    let (n, tip) = fsck(values.iter()).unwrap();
    assert_eq!(n, 3, "crash event not duplicated");
    assert_eq!(tip, h3, "tip continues from the crash-written event");

    // Index healed: tip now h3.
    let healed = index.snapshot(&sid).await.unwrap();
    assert_eq!(healed.tip_hash.as_deref(), Some(h3.as_str()));

    memoryd::sweep::hard_delete(&sid, &store, &index, Some("test-owner"))
        .await
        .unwrap();
    let _ = GENESIS;
}

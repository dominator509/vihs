//! Integration: rebuild-index (EP-003 M4, ADR-003).
//!
//! Flush Redis, run `rebuild_index`, assert the index equals the pre-flush
//! snapshot. Redis is disposable — this is the recovery path.

use std::sync::Arc;

use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::rebuild::rebuild_index;
use memoryd::store::S3Store;
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
    std::env::set_var(
        "VIHS_TOKEN_PEPPER",
        std::env::var("VIHS_TOKEN_PEPPER").unwrap_or_else(|_| "test-memoryd-pepper".into()),
    );
    Config::from_env()
}

fn utt(sid: &SessionId, turn: u64, role: &str, text: &str) -> serde_json::Value {
    json!({
        "v": 1, "session_id": sid.as_str(), "turn_id": turn,
        "ts": "2026-07-05T09:01:00.000Z", "role": role, "kind": "utterance",
        "text": text, "meta": {"asr_conf_bp": 9400, "interrupted": false}
    })
}

#[tokio::test]
async fn rebuild_reconstructs_index_from_store() {
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
    // Mirror the orchestrator's create flow (SPEC-005 A1): a system note
    // carrying meta.owner is the FIRST event — rebuild must restore the
    // owner binding from it (EP-007 M3 GAP-M3-3).
    let create_note = json!({
        "v": 1, "session_id": sid.as_str(), "turn_id": 0,
        "ts": "2026-07-05T09:00:00.000Z", "role": "system", "kind": "note",
        "text": "session created",
        "meta": {"interrupted": false, "owner": "test-owner"}
    });
    handle.append(create_note).await.unwrap();
    handle.append(utt(&sid, 1, "user", "one")).await.unwrap();
    handle
        .append(utt(&sid, 1, "assistant", "two"))
        .await
        .unwrap();
    handle.append(utt(&sid, 2, "user", "three")).await.unwrap();

    let before = index.snapshot(&sid).await.unwrap();
    assert_eq!(before.event_count, Some(4));
    assert_eq!(before.owner.as_deref(), Some("test-owner"));
    assert!(before.tip_hash.is_some());

    // FLUSH Redis — the cache is gone; the store is untouched.
    index.wipe().await.unwrap();
    assert!(index.snapshot(&sid).await.is_err());

    // Rebuild from the object store.
    rebuild_index(&store, &index).await;

    let after = index.snapshot(&sid).await.unwrap();
    assert_eq!(after.event_count, before.event_count, "event count rebuilt");
    assert_eq!(after.tip_hash, before.tip_hash, "tip rebuilt");
    assert_eq!(after.last_turn_id, before.last_turn_id, "last turn rebuilt");
    assert_eq!(after.seq, before.seq, "seq rebuilt");
    // GAP-M3-3: the owner binding MUST survive Redis loss + rebuild, or the
    // user 404s their own session (SPEC-005 A1 / SPEC-006 recovery row).
    assert_eq!(
        after.owner.as_deref(),
        Some("test-owner"),
        "owner restored from create note after rebuild"
    );

    memoryd::sweep::hard_delete(&sid, &store, &index, Some("test-owner"))
        .await
        .unwrap();
}

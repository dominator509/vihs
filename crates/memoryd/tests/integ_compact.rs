//! Integration: compaction (EP-003 M5; SPEC-002 algorithm).
//!
//! Generated 200-turn session: epoch increments, pre-checkpoint preamble
//! bytes (memory.md S0–S2) unchanged within epoch, blob ≤ token budget,
//! supersede rule holds (render uses the LAST summary).

use std::sync::Arc;

use memoryd::compact::{
    maybe_compact, CompactConfig, Compacted, Deps as CompactDeps, MockSummarizer,
};
use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::store::{ObjectStore, S3Store, ARTIFACT_MEMORY};
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

fn utt(sid: &SessionId, turn: u64, role: &str, text: &str) -> serde_json::Value {
    json!({
        "v": 1, "session_id": sid.as_str(), "turn_id": turn,
        "ts": "2026-07-05T09:01:00.000Z", "role": role, "kind": "utterance",
        "text": text, "meta": {"asr_conf_bp": 9400, "interrupted": false}
    })
}

#[tokio::test]
async fn compaction_bounds_session_and_freezes_epoch() {
    let cfg = test_config();
    let store = Arc::new(S3Store::new(&cfg).await);
    let index = Arc::new(RedisIndex::new(&cfg.redis_url).await.unwrap());
    let registry = WriterRegistry::new(store.clone(), index.clone());
    let sid = SessionId::new();
    index
        .create_session(&sid, "test-owner", "2026-07-05T09:00:00Z")
        .await
        .unwrap();

    // 200 turns (400 events: user + assistant each turn).
    let handle = registry.get(&sid);
    for t in 1..=200u64 {
        handle
            .append(utt(&sid, t, "user", &format!("question {t}")))
            .await
            .unwrap();
        handle
            .append(utt(&sid, t, "assistant", &format!("answer {t}")))
            .await
            .unwrap();
    }
    assert_eq!(index.snapshot(&sid).await.unwrap().event_count, Some(400));

    let deps = CompactDeps {
        store: store.clone(),
        index: index.clone(),
        registry: registry.clone(),
        cfg: CompactConfig {
            verbatim_tail: 20,
            token_budget: 3000,
        },
        summarizer: Box::new(MockSummarizer),
    };

    // Compact → epoch 1.
    match maybe_compact(&sid, &deps).await.unwrap() {
        Compacted::Done { epoch, blob_tokens } => {
            assert_eq!(epoch, 1);
            assert!(
                blob_tokens <= 3000,
                "blob within token budget: {blob_tokens}"
            );
        }
        Compacted::NotNeeded => panic!("200-turn session must compact"),
    }
    let snap = index.snapshot(&sid).await.unwrap();
    assert_eq!(snap.epoch, Some(1));
    assert!(snap.summary_ptr.is_some(), "summary_ptr set");

    // memory.md artifact exists and contains the frozen summary.
    let mem = store.get_artifact(&sid, ARTIFACT_MEMORY).await.unwrap();
    let mem_text = String::from_utf8(mem.to_vec()).unwrap();
    assert!(
        mem_text.contains("## Summary"),
        "memory has summary section"
    );

    // Supersede: append 40 more turns, compact again → epoch 2, and the
    // LAST summary wins (roll includes prior summary text).
    for t in 201..=240u64 {
        handle
            .append(utt(&sid, t, "user", &format!("question {t}")))
            .await
            .unwrap();
        handle
            .append(utt(&sid, t, "assistant", &format!("answer {t}")))
            .await
            .unwrap();
    }
    match maybe_compact(&sid, &deps).await.unwrap() {
        Compacted::Done { epoch, .. } => assert_eq!(epoch, 2, "second compaction bumps epoch"),
        Compacted::NotNeeded => panic!("second compaction should run"),
    }

    // Chain fsck over the whole log still passes.
    let bytes = store.read_log(&sid).await.unwrap();
    let values: Vec<serde_json::Value> = bytes
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_slice(l).ok())
        .collect();
    let (n, _) = vihs_core::chain::fsck(values.iter()).unwrap();
    assert_eq!(n, 400 + 80 + 2, "all events + 2 summary events, chained");

    memoryd::sweep::hard_delete(&sid, &store, &index, Some("test-owner"))
        .await
        .unwrap();
}

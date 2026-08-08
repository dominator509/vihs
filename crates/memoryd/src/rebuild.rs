//! Rebuild-index (SPEC-002 Rebuild-index; ADR-003).
//!
//! Streams every `sessions/*/events/*`, fscks the chain, recomputes the
//! Redis hash fields from the log, and refreshes derived artifacts (INV-5).
//! Redis is disposable — this is the recovery path.

use std::sync::Arc;

use tracing::error;
use vihs_core::event::Event;

use crate::index::RedisIndex;
use crate::store::{ObjectStore, S3Store, ARTIFACT_TRANSCRIPT};

/// Extract the session owner from the log's create note (the orchestrator's
/// session-create appends a system note carrying `meta.owner`). Owner binding
/// is restored on rebuild so SPEC-005 A1 survives Redis loss (EP-007 M3).
pub fn owner_from_log(values: &[serde_json::Value]) -> Option<String> {
    values.iter().find_map(|v| {
        if v["kind"].as_str() == Some("note") && v["role"].as_str() == Some("system") {
            v["meta"]["owner"].as_str().map(|o| o.to_string())
        } else {
            None
        }
    })
}

pub async fn rebuild_index(store: &Arc<S3Store>, index: &Arc<RedisIndex>) {
    let sessions = match store.list_sessions().await {
        Ok(s) => s,
        Err(e) => {
            error!("rebuild: list sessions failed: {e}");
            return;
        }
    };
    for sid in sessions {
        let bytes = match store.read_log(&sid).await {
            Ok(b) => b,
            Err(_) => continue,
        };
        let values: Vec<serde_json::Value> = bytes
            .split(|b| *b == b'\n')
            .filter(|l| !l.is_empty())
            .filter_map(|l| serde_json::from_slice(l).ok())
            .collect();
        if values.is_empty() {
            continue;
        }
        match vihs_core::chain::fsck(values.iter()) {
            Ok((count, tip)) => {
                let last_turn = values
                    .iter()
                    .map(|v| v["turn_id"].as_u64().unwrap_or(0))
                    .max()
                    .unwrap_or(0);
                let owner = owner_from_log(&values);
                let seq = store.max_seq(&sid).await.unwrap_or(None).unwrap_or(0);
                if let Err(e) = index
                    .heal(&sid, &tip, last_turn, count, seq, owner.as_deref())
                    .await
                {
                    error!("rebuild: heal failed for {sid}: {e}");
                    continue;
                }
                // Re-render the transcript artifact (INV-5) so rebuild leaves
                // caches fresh; render_memory is refreshed by compaction.
                let events: Vec<Event> = values
                    .iter()
                    .filter_map(|v| serde_json::from_value(v.clone()).ok())
                    .collect();
                let md = vihs_core::render::render_transcript(&events);
                if let Err(e) = store
                    .put_artifact(&sid, ARTIFACT_TRANSCRIPT, md.as_bytes())
                    .await
                {
                    error!("rebuild: artifact write failed for {sid}: {e}");
                }
            }
            Err(e) => error!("rebuild: fsck failed for {sid}: {e}"),
        }
    }
}

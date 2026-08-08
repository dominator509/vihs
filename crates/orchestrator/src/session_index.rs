//! Owner session index (GET /v1/sessions). In-memory; the durable record is
//! memoryd — this index is rebuildable from memoryd's owner zset on restart
//! (EP-010), so it is a cache, not a source of truth.

use std::collections::BTreeMap;
use std::sync::Arc;

use tokio::sync::RwLock;

#[derive(Debug, Clone, serde::Serialize)]
pub struct SessionMeta {
    pub session_id: String,
    pub updated_at: String,
    pub turns: u64,
}

#[derive(Default)]
pub struct SessionIndex {
    /// owner → (session_id → meta)
    inner: RwLock<BTreeMap<String, BTreeMap<String, SessionMeta>>>,
}

impl SessionIndex {
    pub fn new() -> Arc<Self> {
        Arc::new(SessionIndex::default())
    }

    pub async fn upsert(&self, owner: &str, meta: SessionMeta) {
        let mut inner = self.inner.write().await;
        inner
            .entry(owner.to_string())
            .or_default()
            .insert(meta.session_id.clone(), meta);
    }

    pub async fn remove(&self, owner: &str, session_id: &str) {
        let mut inner = self.inner.write().await;
        if let Some(map) = inner.get_mut(owner) {
            map.remove(session_id);
        }
    }

    /// Newest first (updated_at desc).
    pub async fn list(&self, owner: &str) -> Vec<SessionMeta> {
        let inner = self.inner.read().await;
        let mut all: Vec<SessionMeta> = inner
            .get(owner)
            .map(|m| m.values().cloned().collect())
            .unwrap_or_default();
        all.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
        all
    }

    pub async fn get(&self, owner: &str, session_id: &str) -> Option<SessionMeta> {
        let inner = self.inner.read().await;
        inner.get(owner).and_then(|m| m.get(session_id)).cloned()
    }

    /// Warm the in-memory index from Redis on startup (EP-007 M3 GAP-M3-4;
    /// documented in the header: rebuildable from memoryd's owner zset).
    /// The orchestrator's index is a CACHE — after a restart it must be
    /// repopulated from the durable `owner:{owner}:sessions` zsets and
    /// `session:{sid}` hashes (SPEC-002), or every surviving session 404s
    /// at the orchestrator layer even though memoryd has it.
    pub async fn warm_from_redis(&self, redis_url: &str) -> Result<usize, String> {
        let client = redis::Client::open(redis_url).map_err(|e| e.to_string())?;
        let mut conn = client
            .get_connection_manager()
            .await
            .map_err(|e| e.to_string())?;

        // Scan owner zsets (SPEC-002 `owner:{owner_id}:sessions`).
        let owner_keys: Vec<String> = redis::cmd("KEYS")
            .arg("owner:*:sessions")
            .query_async(&mut conn)
            .await
            .map_err(|e| format!("scan owner zsets: {e}"))?;

        let mut restored = 0usize;
        for zkey in owner_keys {
            let owner = zkey
                .strip_prefix("owner:")
                .and_then(|s| s.strip_suffix(":sessions"))
                .unwrap_or_default()
                .to_string();
            if owner.is_empty() {
                continue;
            }
            let sids: Vec<String> = redis::cmd("ZRANGE")
                .arg(&zkey)
                .arg(0)
                .arg(-1)
                .query_async(&mut conn)
                .await
                .map_err(|e| format!("zrange {zkey}: {e}"))?;
            for sid in sids {
                let hkey = format!("session:{sid}");
                let hash: std::collections::HashMap<String, String> = redis::cmd("HGETALL")
                    .arg(&hkey)
                    .query_async(&mut conn)
                    .await
                    .map_err(|e| format!("hgetall {hkey}: {e}"))?;
                let updated_at = hash
                    .get("updated_at")
                    .cloned()
                    .unwrap_or_else(|| chrono::Utc::now().to_rfc3339());
                let turns = hash
                    .get("last_turn_id")
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(0);
                self.upsert(
                    &owner,
                    SessionMeta {
                        session_id: sid,
                        updated_at,
                        turns,
                    },
                )
                .await;
                restored += 1;
            }
        }
        if restored > 0 {
            tracing::info!("warmed session index: restored {restored} sessions");
        }
        Ok(restored)
    }
}

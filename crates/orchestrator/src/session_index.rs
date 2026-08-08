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
}

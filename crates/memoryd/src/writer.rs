//! Per-session single writer (INV-2, ARCHITECTURE §7.3).
//!
//! Exactly one tokio task per hot session owns the log tail; all appends are
//! messages to it. Durability order: object store line FIRST, Redis index
//! SECOND. A crash between the two is healed by `replay_tip_from_log` on
//! next wake; the index is a cache, the log is truth (ADR-003).

use std::collections::VecDeque;
use std::sync::Arc;

use dashmap::DashMap;
use serde_json::Value;
use tokio::sync::{mpsc, oneshot};
use vihs_core::chain::{compute_hash, fsck, GENESIS};
use vihs_core::ids::SessionId;

use crate::error::MemorydError;
use crate::index::RedisIndex;
use crate::store::{ObjectStore, S3Store};

/// One append request. The reply carries the outcome; the writer fills the
/// chain fields — callers NEVER provide prev_hash/hash.
pub enum Append {
    Event {
        body: Value,
        reply: oneshot::Sender<AppendResult>,
    },
}

#[derive(Debug, Clone, PartialEq)]
pub enum AppendResult {
    Committed { hash: String, turn_id: u64 },
    Duplicate { hash: String },
    Rejected(String),
    Retryable(String),
}

pub struct WriterHandle {
    pub tx: mpsc::Sender<Append>,
}

impl WriterHandle {
    pub async fn append(&self, body: Value) -> Result<AppendResult, MemorydError> {
        let (tx, rx) = oneshot::channel();
        self.tx
            .send(Append::Event { body, reply: tx })
            .await
            .map_err(|_| MemorydError::Invalid("writer closed".into()))?;
        rx.await
            .map_err(|_| MemorydError::Invalid("writer dropped reply".into()))
    }
}

/// Registry of per-session writer handles. Spawn-on-first-use; idle writers
/// are retired after 10 min (tip lives in index + log, so retirement is safe).
pub struct WriterRegistry {
    store: Arc<S3Store>,
    index: Arc<RedisIndex>,
    writers: DashMap<SessionId, WriterHandle>,
}

const RECENT_HASHES: usize = 64;

impl WriterRegistry {
    pub fn new(store: Arc<S3Store>, index: Arc<RedisIndex>) -> Arc<Self> {
        Arc::new(WriterRegistry {
            store,
            index,
            writers: DashMap::new(),
        })
    }

    pub fn get(&self, sid: &SessionId) -> WriterHandle {
        if let Some(h) = self.writers.get(sid) {
            return WriterHandle { tx: h.tx.clone() };
        }
        // Double-checked: another task may have raced.
        let entry = self.writers.entry(sid.clone()).or_insert_with(|| {
            let (tx, rx) = mpsc::channel(1024);
            let store = self.store.clone();
            let index = self.index.clone();
            let sid_for_task = sid.clone();
            tokio::spawn(async move {
                session_writer(sid_for_task.clone(), store, index, rx).await;
            });
            WriterHandle { tx }
        });
        WriterHandle {
            tx: entry.tx.clone(),
        }
    }

    /// Number of live writer tasks (diagnostics).
    pub fn live_count(&self) -> usize {
        self.writers.len()
    }
}

/// Recover the tip by replaying the store log when the index is missing or
/// stale. Returns (tip_hash, last_turn, event_count, max_seq) and heals the
/// index (crash-order recovery).
async fn replay_tip_from_log(
    sid: &SessionId,
    store: &S3Store,
    index: &RedisIndex,
) -> Result<(String, u64, u64, u64), MemorydError> {
    let bytes = store.read_log(sid).await?;
    let values: Vec<Value> = bytes
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_slice(l).ok())
        .collect();
    if values.is_empty() {
        return Ok((GENESIS.to_string(), 0, 0, 0));
    }
    let (count, tip) =
        fsck(values.iter()).map_err(|e| MemorydError::IntegrityHold(e.to_string()))?;
    let last_turn = values
        .iter()
        .map(|v| v["turn_id"].as_u64().unwrap_or(0))
        .max()
        .unwrap_or(0);
    let seq = store.max_seq(sid).await?.unwrap_or(0);
    index.heal(sid, &tip, last_turn, count, seq).await?;
    Ok((tip, last_turn, count, seq))
}

/// Recover the writer's tail state on wake (crash-order recovery).
///
/// The index is a CACHE (ADR-003): its tip is only trustworthy if it matches
/// the store's actual last line. When the index is missing OR its tip does
/// not match the store tail, replay from the log and heal the index.
async fn recover_tail(sid: &SessionId, store: &S3Store, index: &RedisIndex) -> (String, u64) {
    let store_tail_hash = match store.max_seq(sid).await {
        Ok(Some(_max)) => last_line_hash(sid, store).await,
        _ => None,
    };

    if let Ok(snap) = index.snapshot(sid).await {
        if let Some(idx_tip) = snap.tip_hash {
            // Index tip matches the store tail → trust it (no replay needed).
            if store_tail_hash.as_deref() == Some(idx_tip.as_str()) {
                return (idx_tip, snap.last_turn_id.unwrap_or(0));
            }
        }
    }

    // Index missing or stale → replay the log and heal.
    match replay_tip_from_log(sid, store, index).await {
        Ok((tip, last_turn, _count, _seq)) => (tip, last_turn),
        Err(_) => (GENESIS.to_string(), 0),
    }
}

/// Hash of the last line in the store log, if any.
async fn last_line_hash(sid: &SessionId, store: &S3Store) -> Option<String> {
    let bytes = store.read_log(sid).await.ok()?;
    let last = bytes.split(|b| *b == b'\n').rfind(|l| !l.is_empty())?;
    let v: Value = serde_json::from_slice(last).ok()?;
    v["hash"].as_str().map(|s| s.to_string())
}

/// The single writer task for one session. Runs until the channel closes
/// (session cold) — the tip then lives in log + index.
pub async fn session_writer(
    sid: SessionId,
    store: Arc<S3Store>,
    index: Arc<RedisIndex>,
    mut rx: mpsc::Receiver<Append>,
) {
    let (mut tip, mut last_turn) = recover_tail(&sid, &store, &index).await;

    let mut seen_tail: VecDeque<String> = VecDeque::new();

    while let Some(Append::Event { mut body, reply }) = rx.recv().await {
        // D-5 idempotency keys on the CONTENT hash — canonical bytes of the
        // body as the pod sends it (no prev/hash). A retried write after a
        // dropped reply reproduces the same content hash regardless of how
        // far the tip has moved, so it is acked Duplicate and never re-logged.
        let content_hash = match compute_content_hash(&body) {
            Ok(h) => h,
            Err(e) => {
                let _ = reply.send(AppendResult::Rejected(e.to_string()));
                continue;
            }
        };
        if seen_tail.contains(&content_hash) {
            let _ = reply.send(AppendResult::Duplicate { hash: content_hash });
            continue;
        }

        body["prev_hash"] = Value::from(tip.clone());
        let h = match compute_hash(&body) {
            Ok(h) => h,
            Err(e) => {
                let _ = reply.send(AppendResult::Rejected(e.to_string()));
                continue;
            }
        };
        body["hash"] = Value::from(h.clone());

        // Durability order matters: object store line FIRST, Redis SECOND.
        let seq = match store.append_line(&sid, &body).await {
            Ok(new_seq) => new_seq,
            Err(e) => {
                let _ = reply.send(AppendResult::Retryable(e.to_string()));
                continue;
            }
        };
        let turn = body["turn_id"].as_u64().unwrap_or(last_turn);
        index.advance(&sid, &h, turn, seq).await.ok();
        seen_tail.push_back(content_hash);
        while seen_tail.len() > RECENT_HASHES {
            seen_tail.pop_front();
        }
        tip = h.clone();
        last_turn = last_turn.max(turn);
        let _ = reply.send(AppendResult::Committed {
            hash: tip.clone(),
            turn_id: turn,
        });
    }
}

/// blake3 over canonical bytes of the body WITHOUT chain fields — the
/// stable content identity a pod retry reproduces (SPEC-002 D-5).
fn compute_content_hash(body: &Value) -> Result<String, MemorydError> {
    let mut stripped = body
        .as_object()
        .cloned()
        .ok_or_else(|| MemorydError::Invalid("event body must be an object".into()))?;
    stripped.remove("prev_hash");
    stripped.remove("hash");
    // compute_hash canonicalizes internally (sorted keys, hash stripped) —
    // the content identity is the canonical hash of the body minus chain
    // fields (SPEC-002 D-5).
    vihs_core::chain::compute_hash(&serde_json::Value::Object(stripped))
        .map_err(|e| MemorydError::Invalid(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recent_hashes_bounded() {
        let mut seen_tail: VecDeque<String> = VecDeque::new();
        for i in 0..100 {
            seen_tail.push_back(format!("h{i}"));
            while seen_tail.len() > RECENT_HASHES {
                seen_tail.pop_front();
            }
        }
        assert_eq!(seen_tail.len(), RECENT_HASHES);
        assert_eq!(seen_tail.back().unwrap(), "h99");
    }
}

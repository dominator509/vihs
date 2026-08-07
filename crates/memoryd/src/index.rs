//! Redis session index — a HOT CACHE, rebuildable from the object store
//! (ADR-003). The object store is truth; Redis failure degrades latency,
//! never data. Key shapes are the SPEC-002 registry — do not invent keys.

use chrono::Utc;
use redis::aio::ConnectionManager;
use redis::AsyncCommands;

use crate::error::IndexErr;
use vihs_core::ids::SessionId;

/// Redis key prefix helpers (SPEC-002 registry).
fn session_key(sid: &SessionId) -> String {
    format!("session:{sid}")
}
fn owner_sessions_key(owner: &str) -> String {
    format!("owner:{owner}:sessions")
}

#[derive(Debug, Clone, Default)]
pub struct SessionSnapshot {
    pub owner: Option<String>,
    pub created_at: Option<String>,
    pub last_turn_id: Option<u64>,
    pub event_count: Option<u64>,
    pub tip_hash: Option<String>,
    pub epoch: Option<u64>,
    pub summary_ptr: Option<String>,
    pub live_pod: Option<String>,
    pub content_hash: Option<String>,
    pub updated_at: Option<String>,
    pub seq: Option<u64>,
}

pub struct RedisIndex {
    conn: ConnectionManager,
}

impl RedisIndex {
    pub async fn new(url: &str) -> Result<Self, IndexErr> {
        let client = redis::Client::open(url).map_err(|e| IndexErr::Redis(e.to_string()))?;
        let conn = ConnectionManager::new(client)
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        Ok(RedisIndex { conn })
    }

    /// Register a new session (create path). Returns the index snapshot.
    pub async fn create_session(
        &self,
        sid: &SessionId,
        owner: &str,
        now: &str,
    ) -> Result<(), IndexErr> {
        let mut conn = self.conn.clone();
        let key = session_key(sid);
        conn.hset_multiple::<&str, &str, &str, ()>(
            &key,
            &[
                ("owner", owner),
                ("created_at", now),
                ("last_turn_id", "0"),
                ("event_count", "0"),
                ("epoch", "0"),
                ("updated_at", now),
            ],
        )
        .await
        .map_err(|e| IndexErr::Redis(e.to_string()))?;
        let _: i64 = conn
            .zadd(
                owner_sessions_key(owner),
                sid.as_str(),
                Utc::now().timestamp(),
            )
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        Ok(())
    }

    pub async fn snapshot(&self, sid: &SessionId) -> Result<SessionSnapshot, IndexErr> {
        let mut conn = self.conn.clone();
        let key = session_key(sid);
        let exists: bool = conn
            .exists(&key)
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        if !exists {
            return Err(IndexErr::Missing(sid.to_string()));
        }
        let fields: Vec<String> = conn
            .hgetall(&key)
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        let mut map = std::collections::HashMap::new();
        for chunk in fields.chunks(2) {
            if chunk.len() == 2 {
                map.insert(chunk[0].clone(), chunk[1].clone());
            }
        }
        Ok(SessionSnapshot {
            owner: map.get("owner").cloned(),
            created_at: map.get("created_at").cloned(),
            last_turn_id: map.get("last_turn_id").and_then(|v| v.parse().ok()),
            event_count: map.get("event_count").and_then(|v| v.parse().ok()),
            tip_hash: map.get("tip_hash").cloned(),
            epoch: map.get("epoch").and_then(|v| v.parse().ok()),
            summary_ptr: map.get("summary_ptr").cloned(),
            live_pod: map.get("live_pod").cloned(),
            content_hash: map.get("content_hash").cloned(),
            updated_at: map.get("updated_at").cloned(),
            seq: map.get("seq").and_then(|v| v.parse().ok()),
        })
    }

    /// Durability order: store line FIRST, index SECOND (ADR-003). `seq` is
    /// the object sequence of the just-appended line.
    pub async fn advance(
        &self,
        sid: &SessionId,
        hash: &str,
        turn_id: u64,
        seq: u64,
    ) -> Result<(), IndexErr> {
        let mut conn = self.conn.clone();
        let key = session_key(sid);
        conn.hset_multiple::<&str, &str, &str, ()>(
            &key,
            &[
                ("tip_hash", hash),
                ("last_turn_id", &turn_id.to_string()),
                ("seq", &seq.to_string()),
                ("updated_at", &Utc::now().to_rfc3339()),
            ],
        )
        .await
        .map_err(|e| IndexErr::Redis(e.to_string()))?;
        let _: i64 = conn
            .hincr(&key, "event_count", 1)
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        Ok(())
    }

    /// Heal the index from a verified log replay (crash recovery / rebuild).
    pub async fn heal(
        &self,
        sid: &SessionId,
        tip: &str,
        last_turn: u64,
        event_count: u64,
        seq: u64,
    ) -> Result<(), IndexErr> {
        let mut conn = self.conn.clone();
        let key = session_key(sid);
        conn.hset_multiple::<&str, &str, &str, ()>(
            &key,
            &[
                ("tip_hash", tip),
                ("last_turn_id", &last_turn.to_string()),
                ("event_count", &event_count.to_string()),
                ("seq", &seq.to_string()),
                ("updated_at", &Utc::now().to_rfc3339()),
            ],
        )
        .await
        .map_err(|e| IndexErr::Redis(e.to_string()))?;
        Ok(())
    }

    pub async fn advance_epoch(
        &self,
        sid: &SessionId,
        hash: &str,
        epoch: u64,
    ) -> Result<(), IndexErr> {
        let mut conn = self.conn.clone();
        let key = session_key(sid);
        conn.hset_multiple::<&str, &str, &str, ()>(
            &key,
            &[("summary_ptr", hash), ("epoch", &epoch.to_string())],
        )
        .await
        .map_err(|e| IndexErr::Redis(e.to_string()))?;
        Ok(())
    }

    pub async fn delete(&self, sid: &SessionId, owner: Option<&str>) -> Result<(), IndexErr> {
        let mut conn = self.conn.clone();
        let key = session_key(sid);
        let _: i64 = conn
            .del(&key)
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        if let Some(owner) = owner {
            let _: i64 = conn
                .zrem(owner_sessions_key(owner), sid.as_str())
                .await
                .map_err(|e| IndexErr::Redis(e.to_string()))?;
        }
        Ok(())
    }

    /// Rebuildable-cache proof: wipe the whole index (rebuild test).
    pub async fn wipe(&self) -> Result<(), IndexErr> {
        let mut conn = self.conn.clone();
        let keys: Vec<String> = conn
            .keys("session:*")
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        if !keys.is_empty() {
            let _: i64 = conn
                .del(&keys)
                .await
                .map_err(|e| IndexErr::Redis(e.to_string()))?;
        }
        let owners: Vec<String> = conn
            .keys("owner:*:sessions")
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        if !owners.is_empty() {
            let _: i64 = conn
                .del(&owners)
                .await
                .map_err(|e| IndexErr::Redis(e.to_string()))?;
        }
        Ok(())
    }
}

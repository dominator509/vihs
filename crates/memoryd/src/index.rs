//! Redis session index — a HOT CACHE, rebuildable from the object store
//! (ADR-003). The object store is truth; Redis failure degrades latency,
//! never data. Key shapes are the SPEC-002 registry — do not invent keys.

use chrono::Utc;
use redis::aio::{ConnectionManager, ConnectionManagerConfig};
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
    /// Keep the client for readiness probes: a FRESH short-lived connection
    /// with explicit timeouts is the only honest way to test reachability
    /// across a Redis restart (the pooled manager holds a half-open socket
    /// for minutes — see `ping` docs).
    client: redis::Client,
}

impl RedisIndex {
    pub async fn new(url: &str) -> Result<Self, IndexErr> {
        let client = redis::Client::open(url).map_err(|e| IndexErr::Redis(e.to_string()))?;
        // Bound data-path waits: with the redis-rs defaults (no response or
        // connection timeout) a dead Redis hangs every command until the OS
        // TCP stack gives up (~112s observed). Fail fast instead — Redis is a
        // hot cache (ADR-003); latency, not availability, is the contract.
        let cfg = ConnectionManagerConfig::new()
            .set_response_timeout(std::time::Duration::from_secs(3))
            .set_connection_timeout(std::time::Duration::from_secs(3));
        let conn = ConnectionManager::new_with_config(client.clone(), cfg)
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        Ok(RedisIndex { conn, client })
    }

    /// Redis reachability probe (SPEC-006 readyz, EP-007 M3).
    ///
    /// Opens a FRESH short-lived connection with explicit 2s timeouts instead
    /// of pinging through the pooled `ConnectionManager`. Why: when Redis dies
    /// (container stop), the pooled TCP socket goes half-open — the PING write
    /// lands in the kernel buffer, the read blocks forever, and redis-rs only
    /// reconnects on `Reconnect`-class errors which never surface until the OS
    /// TCP keepalive gives up (~112s). The pooled readyz therefore HANGS when
    /// Redis is down and takes minutes to recover when it returns. A fresh
    /// connection fails fast (ECONNREFUSED -> 503) and succeeds immediately
    /// once Redis is back — the honest readiness signal.
    pub async fn ping(&self) -> Result<(), IndexErr> {
        let cfg = redis::AsyncConnectionConfig::new()
            .set_connection_timeout(std::time::Duration::from_secs(2))
            .set_response_timeout(std::time::Duration::from_secs(2));
        let mut conn = self
            .client
            .get_multiplexed_async_connection_with_config(&cfg)
            .await
            .map_err(|e| IndexErr::Redis(format!("readyz connect: {e}")))?;
        let _: String = redis::cmd("PING")
            .query_async(&mut conn)
            .await
            .map_err(|e| IndexErr::Redis(format!("readyz ping: {e}")))?;
        Ok(())
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
    /// `owner` is restored from the log's create note (meta.owner) — SPEC-005
    /// A1 requires the owner binding to survive Redis loss (EP-007 M3):
    /// without it, an owner-less record 404s every owner-scoped Load/Delete.
    pub async fn heal(
        &self,
        sid: &SessionId,
        tip: &str,
        last_turn: u64,
        event_count: u64,
        seq: u64,
        owner: Option<&str>,
    ) -> Result<(), IndexErr> {
        let mut conn = self.conn.clone();
        let key = session_key(sid);
        let mut fields: Vec<(&str, String)> = vec![
            ("tip_hash", tip.to_string()),
            ("last_turn_id", last_turn.to_string()),
            ("event_count", event_count.to_string()),
            ("seq", seq.to_string()),
            ("updated_at", Utc::now().to_rfc3339()),
        ];
        let mut zadd: Option<(String, String)> = None;
        if let Some(owner) = owner {
            fields.push(("owner", owner.to_string()));
            fields.push(("created_at", Utc::now().to_rfc3339()));
            zadd = Some((owner_sessions_key(owner), sid.as_str().to_string()));
        }
        let refs: Vec<(&str, &str)> = fields.iter().map(|(k, v)| (*k, v.as_str())).collect();
        conn.hset_multiple::<&str, &str, &str, ()>(&key, &refs)
            .await
            .map_err(|e| IndexErr::Redis(e.to_string()))?;
        if let Some((zkey, member)) = zadd {
            let _: i64 = conn
                .zadd(zkey.as_str(), member.as_str(), Utc::now().timestamp())
                .await
                .map_err(|e| IndexErr::Redis(e.to_string()))?;
        }
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

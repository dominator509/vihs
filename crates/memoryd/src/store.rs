//! Object store client (MinIO/S3). The ONLY code that touches `sessions/`
//! objects (SPEC-002 key registry, D-9).
//!
//! S3 has no true append, so the logical log is maintained as ONE SMALL
//! OBJECT PER APPEND BATCH (ADR-003 companion, EP-003 §7 contract):
//!   sessions/{sid}/events/{seq:012}.jsonl
//! read_log = ordered multi-get concatenation. A compaction task MAY
//! concatenate closed ranges later; appends stay O(1).

use std::time::Duration;

use async_trait::async_trait;
use aws_config::BehaviorVersion;
use aws_sdk_s3::config::{Credentials, Region};
use aws_sdk_s3::presigning::PresigningConfig;
use aws_sdk_s3::primitives::ByteStream;
use aws_sdk_s3::Client;
use bytes::Bytes;
use serde_json::Value;
use url::Url;

use crate::config::Config;
use crate::error::StoreErr;
use vihs_core::ids::SessionId;

pub const EVENTS_PREFIX: &str = "events";
pub const ARTIFACT_TRANSCRIPT: &str = "transcript.md";
pub const ARTIFACT_MEMORY: &str = "memory.md";

#[async_trait]
pub trait ObjectStore: Send + Sync {
    /// Append one sealed event as the next seq object. Returns the seq used.
    async fn append_line(&self, sid: &SessionId, sealed: &Value) -> Result<u64, StoreErr>;
    /// Ordered concatenation of all event objects (logical log).
    async fn read_log(&self, sid: &SessionId) -> Result<Vec<u8>, StoreErr>;
    /// Events with seq > `after_seq` (used by crash recovery tail scan).
    async fn read_log_after(&self, sid: &SessionId, after_seq: u64) -> Result<Vec<u8>, StoreErr>;
    async fn put_artifact(&self, sid: &SessionId, name: &str, bytes: &[u8])
        -> Result<(), StoreErr>;
    async fn get_artifact(&self, sid: &SessionId, name: &str) -> Result<Bytes, StoreErr>;
    async fn sign_get(&self, key: &str, ttl: Duration) -> Result<Url, StoreErr>;
    async fn sign_put(&self, key: &str, ttl: Duration) -> Result<Url, StoreErr>;
    /// Hard delete (D-9): remove every object under the session prefix.
    async fn delete_prefix(&self, sid: &SessionId) -> Result<u64, StoreErr>;
    /// List session ids with any objects (retention sweep).
    async fn list_sessions(&self) -> Result<Vec<SessionId>, StoreErr>;
    /// Highest seq present for a session (writer wake / rebuild).
    async fn max_seq(&self, sid: &SessionId) -> Result<Option<u64>, StoreErr>;
}

pub struct S3Store {
    client: Client,
    bucket: String,
}

impl S3Store {
    pub async fn new(cfg: &Config) -> Self {
        let creds = Credentials::new(
            cfg.s3_access_key.clone(),
            cfg.s3_secret_key.clone(),
            None,
            None,
            "vihs-memoryd",
        );
        let sdk_cfg = aws_config::defaults(BehaviorVersion::latest())
            .region(Region::new("us-east-1"))
            .credentials_provider(creds)
            .endpoint_url(&cfg.s3_endpoint)
            .load()
            .await;
        let client = Client::new(&sdk_cfg);
        S3Store {
            client,
            bucket: cfg.s3_bucket.clone(),
        }
    }

    fn ev_key(sid: &SessionId, seq: u64) -> String {
        format!("sessions/{sid}/{EVENTS_PREFIX}/{seq:012}.jsonl")
    }

    fn art_key(sid: &SessionId, name: &str) -> String {
        format!("sessions/{sid}/{name}")
    }
}

#[async_trait]
impl ObjectStore for S3Store {
    async fn append_line(&self, sid: &SessionId, sealed: &Value) -> Result<u64, StoreErr> {
        let seq = self.max_seq(sid).await?.map(|s| s + 1).unwrap_or(1);
        let key = Self::ev_key(sid, seq);
        let body = serde_json::to_vec(sealed).map_err(|e| StoreErr::Io(e.to_string()))?;
        self.client
            .put_object()
            .bucket(&self.bucket)
            .key(&key)
            .body(ByteStream::from(body))
            .send()
            .await
            .map_err(|e| StoreErr::Io(e.to_string()))?;
        Ok(seq)
    }

    async fn read_log(&self, sid: &SessionId) -> Result<Vec<u8>, StoreErr> {
        let max = self.max_seq(sid).await?;
        let Some(max) = max else {
            return Ok(Vec::new());
        };
        let mut out = Vec::new();
        for seq in 1..=max {
            let key = Self::ev_key(sid, seq);
            match self
                .client
                .get_object()
                .bucket(&self.bucket)
                .key(&key)
                .send()
                .await
            {
                Ok(resp) => {
                    let body = resp
                        .body
                        .collect()
                        .await
                        .map_err(|e| StoreErr::Io(e.to_string()))?
                        .into_bytes();
                    out.extend_from_slice(&body);
                    if !out.ends_with(b"\n") {
                        out.push(b'\n');
                    }
                }
                Err(_) => return Err(StoreErr::NotFound(key)), // gap = torn
            }
        }
        Ok(out)
    }

    async fn read_log_after(&self, sid: &SessionId, after_seq: u64) -> Result<Vec<u8>, StoreErr> {
        let max = self.max_seq(sid).await?;
        let Some(max) = max else {
            return Ok(Vec::new());
        };
        let mut out = Vec::new();
        for seq in (after_seq + 1)..=max {
            let key = Self::ev_key(sid, seq);
            match self
                .client
                .get_object()
                .bucket(&self.bucket)
                .key(&key)
                .send()
                .await
            {
                Ok(resp) => {
                    let body = resp
                        .body
                        .collect()
                        .await
                        .map_err(|e| StoreErr::Io(e.to_string()))?
                        .into_bytes();
                    out.extend_from_slice(&body);
                    if !out.ends_with(b"\n") {
                        out.push(b'\n');
                    }
                }
                Err(_) => return Err(StoreErr::NotFound(key)),
            }
        }
        Ok(out)
    }

    async fn put_artifact(
        &self,
        sid: &SessionId,
        name: &str,
        bytes: &[u8],
    ) -> Result<(), StoreErr> {
        let key = Self::art_key(sid, name);
        self.client
            .put_object()
            .bucket(&self.bucket)
            .key(&key)
            .body(ByteStream::from(bytes.to_vec()))
            .send()
            .await
            .map_err(|e| StoreErr::Io(e.to_string()))?;
        Ok(())
    }

    async fn get_artifact(&self, sid: &SessionId, name: &str) -> Result<Bytes, StoreErr> {
        let key = Self::art_key(sid, name);
        let resp = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(&key)
            .send()
            .await
            .map_err(|_e| StoreErr::NotFound(key.clone()))?;
        resp.body
            .collect()
            .await
            .map(|b| b.into_bytes())
            .map_err(|e| StoreErr::Io(e.to_string()))
    }

    async fn sign_get(&self, key: &str, ttl: Duration) -> Result<Url, StoreErr> {
        let presign =
            PresigningConfig::expires_in(ttl).map_err(|e| StoreErr::Sign(e.to_string()))?;
        let req = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(key)
            .presigned(presign)
            .await
            .map_err(|e| StoreErr::Sign(e.to_string()))?;
        Url::parse(req.uri().to_string().as_str()).map_err(|e| StoreErr::Sign(e.to_string()))
    }

    async fn sign_put(&self, key: &str, ttl: Duration) -> Result<Url, StoreErr> {
        let presign =
            PresigningConfig::expires_in(ttl).map_err(|e| StoreErr::Sign(e.to_string()))?;
        let req = self
            .client
            .put_object()
            .bucket(&self.bucket)
            .key(key)
            .presigned(presign)
            .await
            .map_err(|e| StoreErr::Sign(e.to_string()))?;
        Url::parse(req.uri().to_string().as_str()).map_err(|e| StoreErr::Sign(e.to_string()))
    }

    async fn delete_prefix(&self, sid: &SessionId) -> Result<u64, StoreErr> {
        let prefix = format!("sessions/{sid}/");
        let mut deleted = 0u64;
        let mut token: Option<String> = None;
        loop {
            let mut req = self
                .client
                .list_objects_v2()
                .bucket(&self.bucket)
                .prefix(&prefix);
            if let Some(t) = &token {
                req = req.continuation_token(t);
            }
            let resp = req.send().await.map_err(|e| StoreErr::Io(e.to_string()))?;
            let keys: Vec<String> = resp
                .contents()
                .iter()
                .filter_map(|o| o.key().map(|k| k.to_string()))
                .collect();
            for key in keys {
                self.client
                    .delete_object()
                    .bucket(&self.bucket)
                    .key(&key)
                    .send()
                    .await
                    .map_err(|e| StoreErr::Io(e.to_string()))?;
                deleted += 1;
            }
            if resp.is_truncated().unwrap_or(false) {
                token = resp.next_continuation_token().map(|t| t.to_string());
            } else {
                break;
            }
        }
        Ok(deleted)
    }

    async fn list_sessions(&self) -> Result<Vec<SessionId>, StoreErr> {
        let mut sessions = Vec::new();
        let mut token: Option<String> = None;
        loop {
            let mut req = self
                .client
                .list_objects_v2()
                .bucket(&self.bucket)
                .prefix("sessions/");
            if let Some(t) = &token {
                req = req.continuation_token(t);
            }
            let resp = req.send().await.map_err(|e| StoreErr::Io(e.to_string()))?;
            for o in resp.contents() {
                if let Some(k) = o.key() {
                    // sessions/{sid}/... → sid
                    let rest = k.strip_prefix("sessions/").unwrap_or(k);
                    if let Some(sid) = rest.split('/').next() {
                        if !sid.is_empty() {
                            sessions.push(SessionId(sid.to_string()));
                        }
                    }
                }
            }
            if resp.is_truncated().unwrap_or(false) {
                token = resp.next_continuation_token().map(|t| t.to_string());
            } else {
                break;
            }
        }
        sessions.sort();
        sessions.dedup();
        Ok(sessions)
    }

    async fn max_seq(&self, sid: &SessionId) -> Result<Option<u64>, StoreErr> {
        let prefix = format!("sessions/{sid}/{EVENTS_PREFIX}/");
        let mut max: Option<u64> = None;
        let mut token: Option<String> = None;
        loop {
            let mut req = self
                .client
                .list_objects_v2()
                .bucket(&self.bucket)
                .prefix(&prefix);
            if let Some(t) = &token {
                req = req.continuation_token(t);
            }
            let resp = req.send().await.map_err(|e| StoreErr::Io(e.to_string()))?;
            for o in resp.contents() {
                if let Some(k) = o.key() {
                    let fname = k.rsplit('/').next().unwrap_or("");
                    if let Some(num) = fname.strip_suffix(".jsonl") {
                        if let Ok(seq) = num.parse::<u64>() {
                            max = Some(max.map_or(seq, |m| m.max(seq)));
                        }
                    }
                }
            }
            if resp.is_truncated().unwrap_or(false) {
                token = resp.next_continuation_token().map(|t| t.to_string());
            } else {
                break;
            }
        }
        Ok(max)
    }
}

//! Thin HTTP client for orchestrator → memoryd calls (SPEC-003 memoryd rows
//! the control plane consumes: load, transcript, delete).

use serde_json::{json, Value};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum MemorydClientError {
    #[error("memoryd http {0}")]
    Http(String),
    #[error("memoryd status {0}: {1}")]
    Status(u16, String),
    #[error("memoryd payload: {0}")]
    Payload(String),
}

#[derive(Debug, Clone)]
pub struct MemorydClient {
    base: String,
    client: reqwest::Client,
}

#[derive(Debug, Clone)]
pub struct LoadResult {
    pub tip_hash: String,
    pub last_turn_id: u64,
    pub epoch: u64,
    pub memory_url: Option<String>,
}

impl MemorydClient {
    /// `base` is host:port (ENVIRONMENT.md `VIHS_MEMORYD_ADDR`); the scheme is
    /// implied HTTP for the internal control-plane link.
    pub fn new(base: &str) -> Self {
        let base = base.trim_end_matches('/');
        let base = if base.starts_with("http://") || base.starts_with("https://") {
            base.to_string()
        } else {
            format!("http://{base}")
        };
        MemorydClient {
            base,
            client: reqwest::Client::new(),
        }
    }

    /// POST /v1/sessions — create the durable session row (memoryd index).
    /// `token` is the caller's bearer token, forwarded to memoryd (SPEC-005:
    /// memoryd's authorizer requires a non-empty token even in dev mode).
    pub async fn create_session(
        &self,
        sid: &str,
        owner: &str,
        now: &str,
        token: &str,
    ) -> Result<(), MemorydClientError> {
        // memoryd has no public create route in SPEC-003 (orchestrator owns
        // session creation); this hits the memoryd index via its API seam by
        // appending a system note — which registers owner + timestamp.
        let body = json!({
            "v": 1,
            "session_id": sid,
            "turn_id": 0,
            "ts": now,
            "role": "system",
            "kind": "note",
            "text": "session created",
            "meta": {"interrupted": false, "owner": owner}
        });
        self.append_event(sid, token, &body).await?;
        Ok(())
    }

    /// POST /v1/sessions/{id}/load — fsck-tail + cursor + signed memory URL.
    pub async fn load(&self, sid: &str, token: &str) -> Result<LoadResult, MemorydClientError> {
        let url = format!("{}/v1/sessions/{}/load", self.base, sid);
        let resp = self
            .client
            .post(&url)
            .bearer_auth(token)
            .send()
            .await
            .map_err(|e| MemorydClientError::Http(e.to_string()))?;
        let status = resp.status();
        let body: Value = resp
            .json()
            .await
            .map_err(|e| MemorydClientError::Payload(e.to_string()))?;
        if !status.is_success() {
            return Err(MemorydClientError::Status(
                status.as_u16(),
                body.to_string(),
            ));
        }
        Ok(LoadResult {
            tip_hash: body["cursor"]["tip_hash"]
                .as_str()
                .unwrap_or("")
                .to_string(),
            last_turn_id: body["cursor"]["last_turn_id"].as_u64().unwrap_or(0),
            epoch: body["cursor"]["epoch"].as_u64().unwrap_or(0),
            memory_url: body["memory_url"].as_str().map(|s| s.to_string()),
        })
    }

    /// GET /v1/sessions/{id}/transcript — rendered markdown.
    pub async fn transcript(&self, sid: &str, token: &str) -> Result<String, MemorydClientError> {
        let url = format!("{}/v1/sessions/{}/transcript", self.base, sid);
        let resp = self
            .client
            .get(&url)
            .bearer_auth(token)
            .send()
            .await
            .map_err(|e| MemorydClientError::Http(e.to_string()))?;
        let status = resp.status();
        let text = resp
            .text()
            .await
            .map_err(|e| MemorydClientError::Payload(e.to_string()))?;
        if !status.is_success() {
            return Err(MemorydClientError::Status(status.as_u16(), text));
        }
        Ok(text)
    }

    /// POST /v1/sessions/{id}/events — append an event (pod path).
    pub async fn append_event(
        &self,
        sid: &str,
        token: &str,
        body: &Value,
    ) -> Result<Value, MemorydClientError> {
        let url = format!("{}/v1/sessions/{}/events", self.base, sid);
        let resp = self
            .client
            .post(&url)
            .bearer_auth(token)
            .json(body)
            .send()
            .await
            .map_err(|e| MemorydClientError::Http(e.to_string()))?;
        let status = resp.status();
        let body: Value = resp
            .json()
            .await
            .map_err(|e| MemorydClientError::Payload(e.to_string()))?;
        if !status.is_success() {
            return Err(MemorydClientError::Status(
                status.as_u16(),
                body.to_string(),
            ));
        }
        Ok(body)
    }

    /// DELETE /v1/sessions/{id} — D-9 hard delete.
    pub async fn delete_session(&self, sid: &str, token: &str) -> Result<(), MemorydClientError> {
        let url = format!("{}/v1/sessions/{}", self.base, sid);
        let resp = self
            .client
            .delete(&url)
            .bearer_auth(token)
            .send()
            .await
            .map_err(|e| MemorydClientError::Http(e.to_string()))?;
        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp
                .text()
                .await
                .map_err(|e| MemorydClientError::Payload(e.to_string()))?;
            return Err(MemorydClientError::Status(status.as_u16(), text));
        }
        Ok(())
    }

    /// Helper to build a well-formed event body the writer will seal.
    pub fn event_body(sid: &str, turn_id: u64, role: &str, kind: &str, text: &str) -> Value {
        json!({
            "v": 1,
            "session_id": sid,
            "turn_id": turn_id,
            "ts": chrono::Utc::now().to_rfc3339(),
            "role": role,
            "kind": kind,
            "text": text,
            "meta": {"interrupted": false}
        })
    }
}

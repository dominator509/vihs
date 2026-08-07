//! ID types for VIHS (ARCHITECTURE §4).
//!
//! Four IDs, four lifetimes: `session_id` (durable, UUIDv4), `connection_id`
//! (ephemeral), `pod_id` (ephemeral), `turn_id` (monotonic per session).
//! Only `session_id` and `turn_id` are durable and appear in events.

use serde::{Deserialize, Serialize};
use std::fmt;

/// Durable conversation identifier. Crypto-random UUIDv4. NOT an auth token
/// (SPEC-005): a leaked ID must never replay a transcript.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SessionId(pub String);

impl SessionId {
    pub fn new() -> Self {
        SessionId(uuid::Uuid::new_v4().to_string())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Short display prefix used by renders (e.g. "7f3a").
    pub fn short(&self) -> &str {
        &self.0[..4.min(self.0.len())]
    }
}

impl Default for SessionId {
    fn default() -> Self {
        Self::new()
    }
}

impl fmt::Display for SessionId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Ephemeral connection identifier (one WebRTC transport attempt).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ConnectionId(pub String);

impl ConnectionId {
    pub fn new() -> Self {
        ConnectionId(uuid::Uuid::new_v4().to_string())
    }
}

impl Default for ConnectionId {
    fn default() -> Self {
        Self::new()
    }
}

/// Ephemeral GPU pod identifier.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PodId(pub String);

/// RFC3339 UTC timestamp wrapper. Fixed-width formatting matters for render
/// determinism (render.rs slices [11..16] for HH:MM — never re-parse).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Rfc3339(pub String);

impl Rfc3339 {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

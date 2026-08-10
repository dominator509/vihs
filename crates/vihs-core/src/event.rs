//! Typed event schema mirroring SPEC-002 (D-1..D-7).
//!
//! serde round-trips to the canonical JSON event shape. `v` is the schema
//! version (1). Floats are FORBIDDEN in hashed fields (D-7): `asr_conf` is
//! stored as integer basis points (`asr_conf_bp`, 9400 = 0.94).

use serde::{Deserialize, Serialize};

use crate::ids::{Rfc3339, SessionId};

/// Schema version. Bump per D-6; readers tolerate v±1.
pub const EVENT_V: u32 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    User,
    Assistant,
    System,
    Tool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Utterance,
    ToolCall,
    ToolResult,
    Note,
    Summary,
}

/// Turn range covered by a summary event (D-4): `meta.covers = {from,to}`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Covers {
    pub from_turn: u64,
    pub to_turn: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Meta {
    /// ASR confidence in integer basis points (D-7): 9400 = 0.94.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub asr_conf_bp: Option<u32>,
    /// INV-1: true when the user barged in and this audio was never heard.
    #[serde(default)]
    pub interrupted: bool,
    /// Endpoint → first-audio measured latency, integer ms.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub latency_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub voice: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tokens: Option<u64>,
    /// Present only on `summary` events (D-4).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub covers: Option<Covers>,
    /// Compaction epoch (D-4); monotonically increasing per session.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub epoch: Option<u64>,
    /// Owner binding (SPEC-005 A1): set ONLY on the session-create
    /// bootstrap note (`"session created"`), never on a persona note.
    /// Lets the renderer skip operational notes when picking the persona
    /// name (render.rs persona_name).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub owner: Option<String>,
}

/// One event in the append-only log. Field names are the wire contract —
/// never rename without a D-6 schema bump.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Event {
    pub v: u32,
    pub session_id: SessionId,
    pub turn_id: u64,
    pub ts: Rfc3339,
    pub role: Role,
    pub kind: Kind,
    pub text: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub audio_ref: Option<String>,
    #[serde(default)]
    pub meta: Meta,
    /// Chain field — filled by `chain::seal`, never trusted from callers.
    pub prev_hash: String,
    /// Chain field — recomputed by fsck; `hash` excluded from its own
    /// preimage (ARCHITECTURE §7.2).
    pub hash: String,
}

impl Event {
    pub fn is_utterance(&self) -> bool {
        self.kind == Kind::Utterance
    }

    pub fn is_summary(&self) -> bool {
        self.kind == Kind::Summary
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ids::Rfc3339;

    fn example_json() -> serde_json::Value {
        serde_json::json!({
            "v": 1,
            "session_id": "7f3a1c9e-0000-4000-8000-000000000001",
            "turn_id": 42,
            "ts": "2026-07-07T18:22:31.482Z",
            "role": "assistant",
            "kind": "utterance",
            "text": "Hello! How can I help you today?",
            "audio_ref": "s3://vihs-sessions/sessions/7f3a1c9e-0000-4000-8000-000000000001/turn-42.opus",
            "meta": {
                "asr_conf_bp": 9400,
                "interrupted": false,
                "latency_ms": 812,
                "voice": "aria-v2",
                "tokens": 128
            },
            "prev_hash": "blake3:genesis",
            "hash": "blake3:0000000000000000000000000000000000000000000000000000000000000000"
        })
    }

    #[test]
    fn roundtrip_example() {
        let v = example_json();
        let ev: Event = serde_json::from_value(v.clone()).expect("parse SPEC-002 example");
        let back = serde_json::to_value(&ev).expect("re-serialize");
        // Round-trip must be lossless on the schema-carrying fields.
        assert_eq!(ev.v, 1);
        assert_eq!(ev.turn_id, 42);
        assert_eq!(ev.role, Role::Assistant);
        assert_eq!(ev.kind, Kind::Utterance);
        assert_eq!(ev.meta.asr_conf_bp, Some(9400));
        assert!(!ev.meta.interrupted);
        assert_eq!(ev.meta.latency_ms, Some(812));
        assert_eq!(ev.meta.voice.as_deref(), Some("aria-v2"));
        assert_eq!(ev.meta.tokens, Some(128));
        assert_eq!(ev.prev_hash, "blake3:genesis");
        assert_eq!(back["v"], v["v"]);
        assert_eq!(back["session_id"], v["session_id"]);
        assert_eq!(back["turn_id"], v["turn_id"]);
        assert_eq!(back["ts"], v["ts"]);
        assert_eq!(back["role"], v["role"]);
        assert_eq!(back["kind"], v["kind"]);
        assert_eq!(back["text"], v["text"]);
        assert_eq!(back["meta"]["asr_conf_bp"], v["meta"]["asr_conf_bp"]);
    }

    #[test]
    fn roundtrip_minimal_meta() {
        // Meta fields are defaulted — a minimal event parses.
        let v = serde_json::json!({
            "v": 1,
            "session_id": "7f3a1c9e-0000-4000-8000-000000000001",
            "turn_id": 1,
            "ts": "2026-07-07T18:22:31.482Z",
            "role": "user",
            "kind": "utterance",
            "text": "hi",
            "prev_hash": "blake3:genesis",
            "hash": "x"
        });
        let ev: Event = serde_json::from_value(v).expect("parse minimal");
        assert_eq!(ev.meta, Meta::default());
        assert_eq!(ev.audio_ref, None);
    }

    #[test]
    fn roundtrip_covers() {
        let v = serde_json::json!({
            "v": 1,
            "session_id": "7f3a1c9e-0000-4000-8000-000000000001",
            "turn_id": 30,
            "ts": "2026-07-07T18:22:31.482Z",
            "role": "system",
            "kind": "summary",
            "text": "User is building a suite.",
            "meta": {"covers": {"from_turn": 1, "to_turn": 30}, "epoch": 1},
            "prev_hash": "blake3:genesis",
            "hash": "x"
        });
        let ev: Event = serde_json::from_value(v).expect("parse summary");
        assert_eq!(ev.meta.covers.as_ref().map(|c| c.to_turn), Some(30));
        assert_eq!(ev.meta.epoch, Some(1));
    }

    #[test]
    fn session_id_uuidv4() {
        let sid = SessionId::new();
        assert_eq!(sid.as_str().len(), 36);
        assert_eq!(sid.short().len(), 4);
        let ts = Rfc3339("2026-07-07T18:22:31.482Z".to_string());
        assert_eq!(&ts.as_str()[11..16], "18:22");
    }
}

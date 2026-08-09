//! Signaling relay (SPEC-003): client WS ↔ pod WS, opaque SDP/ICE relay,
//! state frames, strict schema + 16 KiB cap + 3-strike close (SPEC-006).
//!
//! The relay is transport-agnostic at the message layer: frames are JSON
//! objects with a `t` discriminator. A connection is identified by
//! `connection_id`; the orchestrator remembers which pod owns it so `offer`
//! goes pod-ward and `answer`/`ice` come back.

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::ws::{Message, WebSocket};
use serde_json::{json, Value};
use tokio::sync::Mutex;

pub const MAX_FRAME_BYTES: usize = 16 * 1024;
pub const MAX_STRIKES: u32 = 3;

#[derive(Debug, Clone)]
pub struct RelayRoute {
    /// The pod connection id (the pod's own WS identity).
    pub pod_id: String,
    /// The session bound to this connection — the revoke frame needs it.
    pub session_id: String,
}

#[derive(Debug, Clone)]
pub struct RelayHandle {
    /// connection_id → {pod_id, session_id} (the pod's own WS identity)
    pub routes: Arc<Mutex<HashMap<String, RelayRoute>>>,
}

impl RelayHandle {
    pub fn new() -> Arc<Self> {
        Arc::new(RelayHandle {
            routes: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    pub async fn bind(&self, connection_id: &str, pod_id: &str, session_id: &str) {
        self.routes.lock().await.insert(
            connection_id.to_string(),
            RelayRoute {
                pod_id: pod_id.to_string(),
                session_id: session_id.to_string(),
            },
        );
    }

    pub async fn unbind(&self, connection_id: &str) {
        self.routes.lock().await.remove(connection_id);
    }
}

/// Validate a client→orchestrator frame against the SPEC-003 schema.
/// Returns Ok(()) or a strike-worthy reason.
pub fn validate_client_frame(v: &Value) -> Result<(), String> {
    let t = v["t"].as_str().ok_or("missing t")?;
    match t {
        "offer" => {
            if !v["sdp"].is_string() {
                return Err("offer.sdp must be string".into());
            }
        }
        "ice" => {
            if !v["candidate"].is_object() {
                return Err("ice.candidate must be object".into());
            }
        }
        "caption" => {
            // Client→server captions are not part of SPEC-003; reject.
            return Err("caption is server→client only".into());
        }
        _ => return Err(format!("unknown message type: {t}")),
    }
    Ok(())
}

/// SPEC-006 signaling row: classify one inbound client frame as a strike.
/// Returns Ok(()) when the frame is clean, or Err(reason) when it is
/// schema/size abuse. The live signal route counts strikes and closes the
/// WS after MAX_STRIKES (3), matching `client_read_loop`'s contract.
pub fn strike_reason(text: &str, v: Option<&Value>) -> Result<(), String> {
    if text.len() > MAX_FRAME_BYTES {
        return Err(format!("frame exceeds {} bytes", MAX_FRAME_BYTES));
    }
    match v {
        Some(v) => validate_client_frame(v),
        None => Err("invalid JSON".into()),
    }
}

/// Relay one client frame to the pod (or produce a server→client frame).
/// Returns the frame to forward pod-ward, or None if it's terminal.
pub fn client_to_pod(v: &Value) -> Result<Option<Value>, String> {
    validate_client_frame(v)?;
    Ok(Some(v.clone()))
}

/// Build a state frame (server→client).
pub fn state_frame(state: &str) -> Value {
    json!({ "t": "state", "v": state })
}

/// Build the SPEC-003 `revoke` frame pushed to a pod's assign channel when
/// a client's signaling connection ends (EP-007 M4: the pod only releases
/// its assignment slot on revoke — without it, fill never drains and the
/// next connect gets no_capacity).
pub fn revoke_frame(session_id: &str, connection_id: &str) -> Value {
    json!({
        "t": "revoke",
        "session_id": session_id,
        "connection_id": connection_id,
    })
}

/// Build a turn frame when TURN is configured.
pub fn turn_frame(cfg: Option<&crate::config::TurnConfig>) -> Option<Value> {
    cfg.map(|c| {
        json!({ "t": "turn", "cfg": {
            "turn_url": c.turn_url,
            "user": c.turn_user,
            "pass": c.turn_pass,
        }})
    })
}

/// Read loop with cap + strikes for a client connection.
/// Returns when the socket closes or strikes are exhausted.
pub async fn client_read_loop(
    mut ws: WebSocket,
    mut sink: impl FnMut(Message) -> Result<(), String>,
) {
    let mut strikes: u32 = 0;
    while let Some(Ok(msg)) = ws.recv().await {
        match msg {
            Message::Text(text) => {
                if text.len() > MAX_FRAME_BYTES {
                    strikes += 1;
                    if strikes >= MAX_STRIKES {
                        break;
                    }
                    continue;
                }
                match serde_json::from_str::<Value>(&text) {
                    Ok(v) => {
                        if let Err(e) = client_to_pod(&v) {
                            strikes += 1;
                            if strikes >= MAX_STRIKES {
                                break;
                            }
                            let _ = sink(Message::Text(
                                json!({"t":"error","code":"invalid","message":e})
                                    .to_string()
                                    .into(),
                            ));
                        } else {
                            let _ = sink(Message::Text(text));
                        }
                    }
                    Err(_) => {
                        strikes += 1;
                        if strikes >= MAX_STRIKES {
                            break;
                        }
                    }
                }
            }
            Message::Close(_) | Message::Ping(_) | Message::Pong(_) => {}
            Message::Binary(_) => {
                strikes += 1;
                if strikes >= MAX_STRIKES {
                    break;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_validates_offer_and_ice() {
        assert!(validate_client_frame(&json!({"t":"offer","sdp":"v=0"})).is_ok());
        assert!(validate_client_frame(&json!({"t":"ice","candidate":{"a":1}})).is_ok());
        assert!(validate_client_frame(&json!({"t":"bogus"})).is_err());
        assert!(validate_client_frame(&json!({"t":"caption","delta":"x"})).is_err());
    }

    #[test]
    fn strike_reason_clean_frame_ok() {
        let text = r#"{"t":"offer","sdp":"v=0"}"#;
        let v: Value = serde_json::from_str(text).unwrap();
        assert!(strike_reason(text, Some(&v)).is_ok());
    }

    #[test]
    fn strike_reason_invalid_json_is_strike() {
        assert!(strike_reason("not json", None).is_err());
        // Parseable but schema-invalid is also a strike.
        let v: Value = serde_json::from_str(r#"{"t":"bogus"}"#).unwrap();
        assert!(strike_reason(r#"{"t":"bogus"}"#, Some(&v)).is_err());
    }

    #[test]
    fn strike_reason_oversize_frame_is_strike() {
        let big = "x".repeat(MAX_FRAME_BYTES + 1);
        assert!(strike_reason(&big, None).is_err());
        // A small valid frame is fine (the cap is strict `>` on total
        // frame bytes; JSON overhead counts toward it).
        let v: Value = json!({"t":"offer","sdp":"v=0"});
        assert!(strike_reason(r#"{"t":"offer","sdp":"v=0"}"#, Some(&v)).is_ok());
    }

    #[test]
    fn state_frame_shape() {
        let f = state_frame("connected");
        assert_eq!(f["t"], "state");
        assert_eq!(f["v"], "connected");
    }

    #[test]
    fn revoke_frame_shape() {
        let f = revoke_frame("sess-1", "conn-2");
        assert_eq!(f["t"], "revoke");
        assert_eq!(f["session_id"], "sess-1");
        assert_eq!(f["connection_id"], "conn-2");
    }

    #[tokio::test]
    async fn relay_route_roundtrips_pod_and_session() {
        let relay = RelayHandle::new();
        relay.bind("conn-1", "pod-9", "sess-3").await;
        let route = relay.routes.lock().await.get("conn-1").cloned().unwrap();
        assert_eq!(route.pod_id, "pod-9");
        assert_eq!(route.session_id, "sess-3");
        relay.unbind("conn-1").await;
        assert!(relay.routes.lock().await.get("conn-1").is_none());
    }
}

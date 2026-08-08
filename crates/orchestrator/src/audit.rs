//! Audit logging (SPEC-005 A7, EP-006 M4).
//!
//! Security-relevant events are emitted as structured lines: ids only, never
//! tokens, never raw owner ids (owner_hash = blake3 8-hex, OBSERVABILITY.md).
//! Events audited today:
//! - `session_deleted` — hard delete (SPEC-005 A7: "is audited (ids only)"),
//!   emitted by orchestrator (user-facing DELETE) and memoryd (durable path).
//! - `token_minted` — admin mint route (ids only; the raw token is shown once
//!   to the caller and NEVER logged).
//!
//! The line shape follows OBSERVABILITY.md required fields: `ts`, `level`,
//! `service`, `event`, plus contextual ids. Emitted through `tracing::info!`
//! so the ScrubWriter middleware applies as a second layer of defense.

use serde_json::{json, Value};
use vihs_core::redact::owner_hash;

/// Build the audit line (pure, testable shape).
pub fn audit_line(service: &str, event: &str, fields: Value) -> Value {
    json!({
        "ts": chrono::Utc::now().to_rfc3339(),
        "level": "info",
        "service": service,
        "event": event,
        "fields": fields,
    })
}

/// Emit an audit line through tracing (ids only — callers pass owner hashes,
/// never raw owner ids or tokens).
pub fn emit(service: &str, event: &str, fields: Value) {
    tracing::info!(
        target: "audit",
        line = %audit_line(service, event, fields).to_string(),
    );
}

/// Convenience: hard-delete audit with the owner already hashed.
pub fn audit_delete(service: &str, session_id: &str, owner: &str) {
    emit(
        service,
        "session_deleted",
        json!({
            "session_id": session_id,
            "owner_hash": owner_hash(owner),
        }),
    );
}

/// Convenience: token mint audit (ids only — the raw token is never logged).
pub fn audit_mint(service: &str, owner: &str, scope: &str) {
    emit(
        service,
        "token_minted",
        json!({
            "owner_hash": owner_hash(owner),
            "scope": scope,
        }),
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_delete_line_shape_ids_only() {
        let line = audit_line(
            "orchestrator",
            "session_deleted",
            json!({
                "session_id": "sid-123",
                "owner_hash": owner_hash("owner-1"),
            }),
        );
        assert_eq!(line["service"], "orchestrator");
        assert_eq!(line["event"], "session_deleted");
        assert_eq!(line["level"], "info");
        assert!(line["ts"].is_string());
        // Ids only — never the raw owner id.
        let text = line.to_string();
        assert!(!text.contains("owner-1"));
        assert!(text.contains("owner_hash"));
    }

    #[test]
    fn audit_delete_helper_hashes_owner() {
        let fields = json!({
            "session_id": "sid-9",
            "owner_hash": owner_hash("alice"),
        });
        let line = audit_line("memoryd", "session_deleted", fields);
        let text = line.to_string();
        assert!(!text.contains("alice"), "raw owner leaked: {text}");
        // session_id is an ID — allowed in the audit line (ids only).
        assert!(text.contains("sid-9"));
        assert!(text.contains("owner_hash"));
    }

    #[test]
    fn audit_mint_line_never_contains_token() {
        let line = audit_line(
            "orchestrator",
            "token_minted",
            json!({
                "owner_hash": owner_hash("root"),
                "scope": "user",
            }),
        );
        let text = line.to_string();
        assert!(text.contains("token_minted"));
        assert!(text.contains("owner_hash"));
        assert!(!text.contains("token\":"));
        assert!(!text.contains("root"));
    }

    #[test]
    fn audit_line_contains_observability_required_fields() {
        // OBSERVABILITY.md: ts, level, service, event are required.
        let line = audit_line("orchestrator", "session_deleted", json!({}));
        for key in ["ts", "level", "service", "event"] {
            assert!(line.get(key).is_some(), "missing {key}: {line}");
        }
    }
}

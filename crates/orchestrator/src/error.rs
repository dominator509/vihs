//! Error taxonomy for the orchestrator (SPEC-006 envelope shape).

use thiserror::Error;

#[derive(Debug, Error)]
pub enum OrchError {
    #[error("invalid request: {0}")]
    Invalid(String),
    #[error("unauthorized: {0}")]
    Authz(String),
    #[error("session not found: {0}")]
    NotFound(String),
    #[error("integrity hold: {0}")]
    IntegrityHold(String),
    #[error("no capacity: {0}")]
    NoCapacity(String),
    #[error("provider: {0}")]
    Provider(String),
    #[error("upstream: {0}")]
    Upstream(String),
    #[error("rate limited: {0}")]
    RateLimited(String),
}

impl OrchError {
    pub fn code(&self) -> &'static str {
        match self {
            OrchError::Invalid(_) => "invalid",
            OrchError::Authz(_) => "unauthorized",
            OrchError::NotFound(_) => "not_found",
            OrchError::IntegrityHold(_) => "integrity_hold",
            OrchError::NoCapacity(_) => "no_capacity",
            OrchError::Provider(_) => "provider",
            OrchError::Upstream(_) => "upstream",
            OrchError::RateLimited(_) => "rate_limited",
        }
    }
    pub fn retryable(&self) -> bool {
        // SPEC-006 R1: rate_limited is caller-retry with backoff (EP-006 M4).
        matches!(
            self,
            OrchError::NoCapacity(_)
                | OrchError::Provider(_)
                | OrchError::Upstream(_)
                | OrchError::RateLimited(_)
        )
    }
    pub fn status(&self) -> axum::http::StatusCode {
        match self {
            OrchError::Invalid(_) => axum::http::StatusCode::BAD_REQUEST,
            OrchError::Authz(_) => axum::http::StatusCode::UNAUTHORIZED,
            OrchError::NotFound(_) => axum::http::StatusCode::NOT_FOUND,
            OrchError::IntegrityHold(_) => axum::http::StatusCode::CONFLICT,
            OrchError::NoCapacity(_) => axum::http::StatusCode::SERVICE_UNAVAILABLE,
            OrchError::Provider(_) | OrchError::Upstream(_) => {
                axum::http::StatusCode::SERVICE_UNAVAILABLE
            }
            OrchError::RateLimited(_) => axum::http::StatusCode::TOO_MANY_REQUESTS,
        }
    }
}

impl From<crate::memoryd_client::MemorydClientError> for OrchError {
    fn from(e: crate::memoryd_client::MemorydClientError) -> Self {
        // SPEC-006 "Chain verification failure on load": memoryd returns
        // 409 integrity_hold; the orchestrator MUST preserve that on the
        // public resume/connect surface (SPEC-003 resume row) instead of
        // collapsing every memoryd status to Upstream (503). EP-007 M3
        // torn_write_fsck proves this end-to-end.
        if let crate::memoryd_client::MemorydClientError::Status(status, body) = &e {
            if *status == 409 && body.contains("integrity_hold") {
                return OrchError::IntegrityHold(body.clone());
            }
        }
        OrchError::Upstream(e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::memoryd_client::MemorydClientError;

    #[test]
    fn memoryd_409_integrity_hold_maps_to_conflict() {
        // SPEC-003 resume row: 409 integrity_hold on the public surface.
        let err = MemorydClientError::Status(
            409,
            r#"{"error":{"code":"integrity_hold","message":"chain broken","retryable":false}}"#
                .into(),
        );
        let orch: OrchError = err.into();
        assert!(matches!(orch, OrchError::IntegrityHold(_)));
        assert_eq!(orch.code(), "integrity_hold");
        assert_eq!(orch.status(), axum::http::StatusCode::CONFLICT);
        assert!(!orch.retryable());
    }

    #[test]
    fn memoryd_409_other_code_stays_upstream() {
        // A 409 that is NOT integrity_hold must not be misclassified.
        let err = MemorydClientError::Status(
            409,
            r#"{"error":{"code":"other","message":"x","retryable":true}}"#.into(),
        );
        let orch: OrchError = err.into();
        assert!(matches!(orch, OrchError::Upstream(_)));
        assert_eq!(orch.status(), axum::http::StatusCode::SERVICE_UNAVAILABLE);
    }

    #[test]
    fn memoryd_503_stays_upstream() {
        let err = MemorydClientError::Status(
            503,
            r#"{"error":{"code":"retryable","message":"store","retryable":true}}"#.into(),
        );
        let orch: OrchError = err.into();
        assert!(matches!(orch, OrchError::Upstream(_)));
        assert!(orch.retryable());
    }
}

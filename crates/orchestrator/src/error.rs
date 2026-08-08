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
        matches!(
            self,
            OrchError::NoCapacity(_) | OrchError::Provider(_) | OrchError::Upstream(_)
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
        OrchError::Upstream(e.to_string())
    }
}

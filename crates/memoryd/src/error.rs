//! Error taxonomy for memoryd (SPEC-006 shape; retryable split).

use thiserror::Error;

#[derive(Debug, Error)]
pub enum StoreErr {
    #[error("store io: {0}")]
    Io(String),
    #[error("not found: {0}")]
    NotFound(String),
    #[error("signing failed: {0}")]
    Sign(String),
}

impl StoreErr {
    pub fn retryable(&self) -> bool {
        matches!(self, StoreErr::Io(_))
    }
}

#[derive(Debug, Error)]
pub enum IndexErr {
    #[error("redis: {0}")]
    Redis(String),
    #[error("session not indexed: {0}")]
    Missing(String),
}

#[derive(Debug, Error)]
pub enum MemorydError {
    #[error("store: {0}")]
    Store(#[from] StoreErr),
    #[error("index: {0}")]
    Index(#[from] IndexErr),
    #[error("authz: {0}")]
    Authz(#[from] AuthzErr),
    #[error("chain: {0}")]
    Chain(#[from] vihs_core::chain::ChainError),
    #[error("invalid: {0}")]
    Invalid(String),
    #[error("integrity hold: {0}")]
    IntegrityHold(String),
    #[error("session not found: {0}")]
    NotFound(String),
}

#[derive(Debug, Error)]
pub enum AuthzErr {
    #[error("missing token")]
    MissingToken,
    #[error("invalid token")]
    InvalidToken,
    #[error("forbidden")]
    Forbidden,
    /// Foreign-or-unknown session — 404, never 403 (SPEC-005 A4: no ID
    /// oracle; SPEC-006 mapping).
    #[error("session not found")]
    NotFound,
}

//! Token store seam (EP-006 M1; M3 moved the implementation to `vihs-auth`).
//!
//! The shared TokenStore lives in `vihs-auth` so BOTH orchestrator and
//! memoryd verify tokens minted by either (SPEC-005 A1; EP-006 M3 Decision
//! Log). This module re-exports the shared types and bridges its errors into
//! the orchestrator's taxonomy.

pub use vihs_auth::{Principal, Scope, TokenError, TokenStore, DEFAULT_TOKEN_TTL, POD_TOKEN_TTL};

impl From<TokenError> for crate::error::OrchError {
    fn from(e: TokenError) -> Self {
        match e {
            TokenError::Authz(m) => crate::error::OrchError::Authz(m),
            TokenError::Upstream(m) => crate::error::OrchError::Upstream(m),
        }
    }
}

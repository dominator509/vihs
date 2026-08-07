//! Pluggable authorization (SPEC-005 seam). EP-003 ships the permissive dev
//! impl; real bearer-token enforcement lands in EP-006 behind the same trait.

use crate::error::AuthzErr;
use vihs_core::ids::SessionId;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verb {
    Append,
    Load,
    Delete,
    Admin,
}

#[derive(Debug, Clone)]
pub struct Principal {
    pub owner: String,
    pub scope: Scope,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    User,
    Admin,
}

#[async_trait::async_trait]
pub trait Authorizer: Send + Sync {
    fn allow(&self, token: &str, sid: &SessionId, verb: Verb) -> Result<Principal, AuthzErr>;
}

/// Dev impl: any non-empty token passes. EP-006 replaces this.
pub struct PermissiveAuthorizer;

#[async_trait::async_trait]
impl Authorizer for PermissiveAuthorizer {
    fn allow(&self, token: &str, _sid: &SessionId, _verb: Verb) -> Result<Principal, AuthzErr> {
        if token.is_empty() {
            return Err(AuthzErr::MissingToken);
        }
        Ok(Principal {
            owner: "dev".to_string(),
            scope: Scope::User,
        })
    }
}

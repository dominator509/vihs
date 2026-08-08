//! Pluggable authorization (SPEC-005 seam). EP-004 ships the permissive dev
//! impl; real bearer-token enforcement lands in EP-006 behind the same trait.

use crate::error::OrchError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verb {
    Session,
    Admin,
    Pod,
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
    Pod,
}

#[async_trait::async_trait]
pub trait Authorizer: Send + Sync {
    fn allow(&self, token: &str, verb: Verb) -> Result<Principal, OrchError>;
}

/// Dev impl: any non-empty token passes; scope depends on the verb.
/// EP-006 replaces this with real minted tokens.
pub struct PermissiveAuthorizer;

#[async_trait::async_trait]
impl Authorizer for PermissiveAuthorizer {
    fn allow(&self, token: &str, verb: Verb) -> Result<Principal, OrchError> {
        if token.is_empty() {
            return Err(OrchError::Authz("missing token".into()));
        }
        let scope = match verb {
            Verb::Session => Scope::User,
            Verb::Admin => Scope::Admin,
            Verb::Pod => Scope::Pod,
        };
        Ok(Principal {
            owner: "dev".to_string(),
            scope,
        })
    }
}

//! Pluggable authorization (SPEC-005 seam). EP-006 replaces the permissive
//! dev impl with bearer-token enforcement backed by the Redis token store.

use crate::error::OrchError;
use crate::tokens::TokenStore;

pub use vihs_auth::{Principal, Scope};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verb {
    Session,
    Admin,
    Pod,
}

#[async_trait::async_trait]
pub trait Authorizer: Send + Sync {
    async fn allow(&self, token: &str, verb: Verb) -> Result<Principal, OrchError>;
}

/// Real bearer-token authorizer: resolves via the Redis token store, then
/// enforces scope-vs-verb (SPEC-005 A1/A6). Constant-time verify is inside
/// TokenStore::verify (argon2id).
pub struct TokenAuthorizer {
    store: TokenStore,
}

impl TokenAuthorizer {
    pub fn new(store: TokenStore) -> Self {
        TokenAuthorizer { store }
    }
}

#[async_trait::async_trait]
impl Authorizer for TokenAuthorizer {
    async fn allow(&self, token: &str, verb: Verb) -> Result<Principal, OrchError> {
        if token.is_empty() {
            crate::metrics::record_authz_denial();
            return Err(OrchError::Authz("missing token".into()));
        }
        let p = self.store.verify(token).await?;
        let ok = match verb {
            Verb::Session => p.scope == Scope::User || p.scope == Scope::Admin,
            Verb::Admin => p.scope == Scope::Admin,
            Verb::Pod => p.scope == Scope::Pod,
        };
        if !ok {
            crate::metrics::record_authz_denial();
            return Err(OrchError::Authz("scope not permitted for route".into()));
        }
        Ok(p)
    }
}

/// Dev impl: any non-empty token passes; scope depends on the verb.
/// Kept for tests that exercise routing without a token store; EP-006 M2+
/// replaces callers with the real authorizer where auth is under test.
pub struct PermissiveAuthorizer;

#[async_trait::async_trait]
impl Authorizer for PermissiveAuthorizer {
    async fn allow(&self, token: &str, verb: Verb) -> Result<Principal, OrchError> {
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

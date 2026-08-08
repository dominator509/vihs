//! Pluggable authorization (SPEC-005 seam). EP-006 M3 replaces the permissive
//! dev impl with bearer-token enforcement backed by the SHARED Redis token
//! store (`vihs-auth`) + the session index for owner binding (SPEC-005 A1).
//!
//! The authorizer is async because `TokenStore::verify` is a Redis round-trip.

use std::sync::Arc;

use vihs_auth::{Principal, Scope, TokenStore};
use vihs_core::ids::SessionId;

use crate::error::AuthzErr;
use crate::index::RedisIndex;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verb {
    Append,
    Load,
    Delete,
    Admin,
}

#[async_trait::async_trait]
pub trait Authorizer: Send + Sync {
    async fn allow(&self, token: &str, sid: &SessionId, verb: Verb) -> Result<Principal, AuthzErr>;
}

/// Real bearer-token authorizer (SPEC-005 A1/A3/A7):
/// - admin scope: every verb.
/// - pod scope: Append/Load only AND the token's bound owner (the session it
///   was minted for) must equal the path id (A1/A3). Anything else is 404
///   (foreign session — no ID oracle) or 403 (forbidden verb).
/// - user scope: the session record's owner must equal the principal's owner
///   (A1); a MISSING record is the create path — a valid user token may open
///   it with Append, and the handler stamps the owner (the orchestrator's
///   session-create appends a system note with the user's token).
pub struct TokenAuthorizer {
    store: TokenStore,
    index: Arc<RedisIndex>,
}

impl TokenAuthorizer {
    pub fn new(store: TokenStore, index: Arc<RedisIndex>) -> Self {
        TokenAuthorizer { store, index }
    }
}

#[async_trait::async_trait]
impl Authorizer for TokenAuthorizer {
    async fn allow(&self, token: &str, sid: &SessionId, verb: Verb) -> Result<Principal, AuthzErr> {
        if token.is_empty() {
            return Err(AuthzErr::MissingToken);
        }
        let p = self.store.verify(token).await.map_err(|e| match e {
            vihs_auth::TokenError::Authz(_) => AuthzErr::InvalidToken,
            vihs_auth::TokenError::Upstream(m) => AuthzErr::Upstream(m),
        })?;
        let scope = p.scope;
        let owner = p.owner.clone();
        tracing::debug!(
            sid = %sid.as_str(),
            verb = ?verb,
            scope = ?scope,
            owner_hash = %vihs_core::redact::owner_hash(&owner),
            "authz decision",
        );
        let decision = match p.scope {
            Scope::Admin => Ok(p),
            Scope::Pod => {
                // Session-bound pod token: owner IS the session id (A3).
                if p.owner != sid.as_str() {
                    return Err(AuthzErr::NotFound); // foreign session — no oracle
                }
                match verb {
                    Verb::Append | Verb::Load => Ok(p),
                    _ => Err(AuthzErr::Forbidden), // pods never delete/compact-admin
                }
            }
            Scope::User => {
                // Owner route (A1): session.owner must equal principal.owner.
                match self.index.snapshot(sid).await {
                    Ok(snap) => match snap.owner.as_deref() {
                        // Owner-less record = pre-authz legacy or heal-only
                        // (rebuild/recovery — heal does not touch owner). The
                        // create/bind path: a valid user token appending may
                        // claim it; the append handler stamps the owner
                        // (EP-006 M3 Decision Log). Load/Delete still 404 —
                        // no ID oracle, no ownership proof.
                        None if matches!(verb, Verb::Append) => Ok(p),
                        Some(rec_owner) if rec_owner == p.owner.as_str() => Ok(p),
                        _ => Err(AuthzErr::NotFound), // foreign — no oracle
                    },
                    Err(_) => {
                        // No record yet = the CREATE path (orchestrator's
                        // session-create appends a system note with the
                        // caller's user token). Only Append may create.
                        if matches!(verb, Verb::Append) {
                            Ok(p)
                        } else {
                            Err(AuthzErr::NotFound)
                        }
                    }
                }
            }
        };
        tracing::debug!(
            sid = %sid.as_str(),
            verb = ?verb,
            scope = ?scope,
            owner_hash = %vihs_core::redact::owner_hash(&owner),
            "authz result",
        );
        decision
    }
}

/// Dev impl: any non-empty token passes. Retained for storage-level
/// integration tests (integ_*); the strict authorizer is under test in
/// tests/authz.rs.
pub struct PermissiveAuthorizer;

#[async_trait::async_trait]
impl Authorizer for PermissiveAuthorizer {
    async fn allow(
        &self,
        token: &str,
        _sid: &SessionId,
        _verb: Verb,
    ) -> Result<Principal, AuthzErr> {
        if token.is_empty() {
            return Err(AuthzErr::MissingToken);
        }
        Ok(Principal {
            owner: "dev".to_string(),
            scope: Scope::User,
        })
    }
}

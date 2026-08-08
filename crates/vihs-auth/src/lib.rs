//! vihs-auth — shared token infrastructure (EP-006 M3).
//!
//! The opaque-token store (mint/seed/verify/revoke) lives HERE, not in the
//! orchestrator, because memoryd must verify tokens minted by the
//! orchestrator (SPEC-005 A1). A single implementation guarantees:
//!   - one token shape (32-byte base64url, 16-byte id prefix),
//!   - one hash algorithm (argon2id + pepper, OWASP params),
//!   - one pepper source (`VIHS_TOKEN_PEPPER`, shared across services),
//!   - one Redis key scheme (`token:{token_id}` per SPEC-002).
//!
//! Dependency rules (ARCHITECTURE.md §3): both `orchestrator` and `memoryd`
//! may import `vihs-auth`; `vihs-auth` must not import either service crate.

pub mod store;

pub use store::{Principal, Scope, TokenError, TokenStore, DEFAULT_TOKEN_TTL, POD_TOKEN_TTL};

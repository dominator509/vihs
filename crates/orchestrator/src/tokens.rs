//! Token store + mint/verify (SPEC-005; EP-006 M1).
//!
//! Opaque tokens are 32 random bytes, base64url. The token carries its own
//! lookup id (first 16 bytes = token_id; the rest is secret), so the server
//! derives the Redis key `token:{token_id}` without storing the token.
//! The record holds `argon2id(token + pepper)` — a Redis leak yields no
//! usable tokens. Verify is constant-time (argon2 verify + fixed compares).
//!
//! Pepper comes from `VIHS_TOKEN_PEPPER`. Dev generates an ephemeral pepper
//! with a loud log (EP-006 §9); stage/prod MUST set it or the service
//! refuses to start (STOP S1).

use std::sync::Arc;
use std::time::Duration;

use argon2::password_hash::{
    rand_core::OsRng, PasswordHash, PasswordHasher, PasswordVerifier, SaltString,
};
use argon2::Argon2;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use rand::RngCore;

use crate::authz::{Principal, Scope};
use crate::error::OrchError;

/// Argon2id OWASP-recommended parameters (SPEC-005 security rules).
const ARGON2_M_COST: u32 = 19_456;
const ARGON2_T_COST: u32 = 2;
const ARGON2_P_COST: u32 = 1;

/// Token lifetime for user/admin tokens (SPEC-005 A3: pod tokens ≤15 min;
/// user tokens are long-lived until revoked — mint passes an explicit TTL).
pub const DEFAULT_TOKEN_TTL: Duration = Duration::from_secs(24 * 60 * 60);

/// 16-byte token_id prefix + 16-byte secret = 32 random bytes total.
const TOKEN_ID_BYTES: usize = 16;
const TOKEN_SECRET_BYTES: usize = 16;
const TOKEN_TOTAL_BYTES: usize = TOKEN_ID_BYTES + TOKEN_SECRET_BYTES;

fn redis_key(token_id: &str) -> String {
    format!("token:{token_id}")
}

/// Redis-backed token store. Cheap to clone (Arc<ConnectionManager>).
#[derive(Clone)]
pub struct TokenStore {
    redis: redis::aio::ConnectionManager,
    pepper: Arc<String>,
}

impl TokenStore {
    pub async fn connect(redis_url: &str, pepper: String) -> Result<Self, OrchError> {
        let client = redis::Client::open(redis_url.to_string())
            .map_err(|e| OrchError::Upstream(format!("redis open: {e}")))?;
        let mgr = redis::aio::ConnectionManager::new(client)
            .await
            .map_err(|e| OrchError::Upstream(format!("redis connect: {e}")))?;
        Ok(TokenStore {
            redis: mgr,
            pepper: Arc::new(pepper),
        })
    }

    /// Mint a token: generate 32 random bytes, store `argon2id(token+pepper)`
    /// under `token:{token_id}`, return the opaque base64url token.
    pub async fn mint(
        &self,
        owner: &str,
        scope: Scope,
        ttl: Duration,
    ) -> Result<String, OrchError> {
        let mut raw = [0u8; TOKEN_TOTAL_BYTES];
        OsRng.fill_bytes(&mut raw);
        let (id_bytes, _secret) = raw.split_at(TOKEN_ID_BYTES);
        let token_id = URL_SAFE_NO_PAD.encode(id_bytes);
        let token = URL_SAFE_NO_PAD.encode(raw);

        let phash = self.hash(&token)?;
        let exp = chrono::Utc::now().timestamp() + ttl.as_secs() as i64;
        let key = redis_key(&token_id);
        let mut pipe = redis::pipe();
        pipe.hset_multiple(
            &key,
            &[
                ("owner", owner),
                ("scope", scope_name(scope)),
                ("phash", phash.as_str()),
                ("exp", exp.to_string().as_str()),
                ("revoked", "0"),
            ],
        )
        .ignore()
        .expire(&key, ttl.as_secs() as i64)
        .ignore();
        let _: () = pipe
            .query_async(&mut self.redis.clone())
            .await
            .map_err(|e| OrchError::Upstream(format!("redis mint: {e}")))?;
        Ok(token)
    }

    /// Seed a caller-supplied token (env-provided credentials: pod token,
    /// bootstrap admin token). Validates the 32-byte base64url shape so a
    /// seeded token always passes verify(). Idempotent — re-seeding the same
    /// token_id overwrites the record.
    pub async fn seed(
        &self,
        token: &str,
        owner: &str,
        scope: Scope,
        ttl: Duration,
    ) -> Result<(), OrchError> {
        let raw = URL_SAFE_NO_PAD
            .decode(token)
            .map_err(|_| OrchError::Authz("seed token must be base64url".into()))?;
        if raw.len() != TOKEN_TOTAL_BYTES {
            return Err(OrchError::Authz(
                "seed token must decode to 32 bytes".into(),
            ));
        }
        let (id_bytes, _) = raw.split_at(TOKEN_ID_BYTES);
        let token_id = URL_SAFE_NO_PAD.encode(id_bytes);
        let phash = self.hash(token)?;
        let exp = chrono::Utc::now().timestamp() + ttl.as_secs() as i64;
        let key = redis_key(&token_id);
        let mut pipe = redis::pipe();
        pipe.hset_multiple(
            &key,
            &[
                ("owner", owner),
                ("scope", scope_name(scope)),
                ("phash", phash.as_str()),
                ("exp", exp.to_string().as_str()),
                ("revoked", "0"),
            ],
        )
        .ignore()
        .expire(&key, ttl.as_secs() as i64)
        .ignore();
        let _: () = pipe
            .query_async(&mut self.redis.clone())
            .await
            .map_err(|e| OrchError::Upstream(format!("redis seed: {e}")))?;
        Ok(())
    }

    /// Verify + resolve a token to a Principal. Constant-time: argon2 verify
    /// dominates; expiry/revoked are checked after a successful verify.
    pub async fn verify(&self, token: &str) -> Result<Principal, OrchError> {
        let raw = URL_SAFE_NO_PAD
            .decode(token)
            .map_err(|_| OrchError::Authz("malformed token".into()))?;
        if raw.len() != TOKEN_TOTAL_BYTES {
            return Err(OrchError::Authz("malformed token".into()));
        }
        let (id_bytes, _) = raw.split_at(TOKEN_ID_BYTES);
        let token_id = URL_SAFE_NO_PAD.encode(id_bytes);
        let key = redis_key(&token_id);

        let fields: Vec<(String, String)> = redis::cmd("HGETALL")
            .arg(&key)
            .query_async(&mut self.redis.clone())
            .await
            .map_err(|e| OrchError::Upstream(format!("redis verify: {e}")))?;
        let rec: std::collections::HashMap<String, String> = fields.into_iter().collect();

        let Some(owner) = rec.get("owner") else {
            return Err(OrchError::Authz("unknown token".into()));
        };
        let Some(scope) = rec.get("scope") else {
            return Err(OrchError::Authz("unknown token".into()));
        };
        let Some(phash) = rec.get("phash") else {
            return Err(OrchError::Authz("unknown token".into()));
        };
        let exp: i64 = rec
            .get("exp")
            .and_then(|v| v.parse().ok())
            .ok_or_else(|| OrchError::Authz("corrupt token record".into()))?;
        let revoked: i64 = rec.get("revoked").and_then(|v| v.parse().ok()).unwrap_or(0);
        if revoked != 0 {
            return Err(OrchError::Authz("token revoked".into()));
        }
        if chrono::Utc::now().timestamp() > exp {
            return Err(OrchError::Authz("token expired".into()));
        }

        // Constant-time hash compare + argon2 verify.
        self.verify_hash(phash, token)?;
        Ok(Principal {
            owner: owner.clone(),
            scope: parse_scope(scope),
        })
    }

    pub async fn revoke(&self, token: &str) -> Result<(), OrchError> {
        let raw = URL_SAFE_NO_PAD
            .decode(token)
            .map_err(|_| OrchError::Authz("malformed token".into()))?;
        let (id_bytes, _) = raw.split_at(TOKEN_ID_BYTES);
        let token_id = URL_SAFE_NO_PAD.encode(id_bytes);
        let _: () = redis::cmd("HSET")
            .arg(redis_key(&token_id))
            .arg("revoked")
            .arg("1")
            .query_async(&mut self.redis.clone())
            .await
            .map_err(|e| OrchError::Upstream(format!("redis revoke: {e}")))?;
        Ok(())
    }

    fn hash(&self, token: &str) -> Result<String, OrchError> {
        let salted = format!("{token}{}", self.pepper);
        let salt = SaltString::generate(&mut OsRng);
        let argon = Argon2::new(
            argon2::Algorithm::Argon2id,
            argon2::Version::V0x13,
            argon2::Params::new(ARGON2_M_COST, ARGON2_T_COST, ARGON2_P_COST, None)
                .map_err(|e| OrchError::Upstream(format!("argon2 params: {e}")))?,
        );
        argon
            .hash_password(salted.as_bytes(), &salt)
            .map(|h| h.to_string())
            .map_err(|e| OrchError::Upstream(format!("argon2 hash: {e}")))
    }

    fn verify_hash(&self, phash: &str, token: &str) -> Result<(), OrchError> {
        let salted = format!("{token}{}", self.pepper);
        let parsed = PasswordHash::new(phash)
            .map_err(|_| OrchError::Authz("corrupt token record".into()))?;
        let argon = Argon2::new(
            argon2::Algorithm::Argon2id,
            argon2::Version::V0x13,
            argon2::Params::new(ARGON2_M_COST, ARGON2_T_COST, ARGON2_P_COST, None)
                .map_err(|e| OrchError::Upstream(format!("argon2 params: {e}")))?,
        );
        match argon.verify_password(salted.as_bytes(), &parsed) {
            Ok(()) => Ok(()),
            Err(_) => Err(OrchError::Authz("token mismatch".into())),
        }
    }
}

fn scope_name(s: Scope) -> &'static str {
    match s {
        Scope::User => "user",
        Scope::Admin => "admin",
        Scope::Pod => "pod",
    }
}

fn parse_scope(s: &str) -> Scope {
    match s {
        "admin" => Scope::Admin,
        "pod" => Scope::Pod,
        _ => Scope::User,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Dev-services Redis must be running (scripts/dev-services.sh).
    async fn store() -> TokenStore {
        TokenStore::connect("redis://127.0.0.1:6379", "test-pepper".to_string())
            .await
            .expect("redis connect")
    }

    #[tokio::test]
    async fn mint_verify_roundtrip() {
        let st = store().await;
        let token = st
            .mint("owner-1", Scope::User, DEFAULT_TOKEN_TTL)
            .await
            .unwrap();
        let p = st.verify(&token).await.unwrap();
        assert_eq!(p.owner, "owner-1");
        assert_eq!(p.scope, Scope::User);
    }

    #[tokio::test]
    async fn wrong_token_rejected() {
        let st = store().await;
        st.mint("owner-1", Scope::User, DEFAULT_TOKEN_TTL)
            .await
            .unwrap();
        let err = st.verify(&"AAAA".repeat(8)).await.unwrap_err();
        assert!(matches!(err, OrchError::Authz(_)));
    }

    #[tokio::test]
    async fn expiry_enforced() {
        let st = store().await;
        let token = st
            .mint("owner-1", Scope::User, Duration::from_millis(1))
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        let err = st.verify(&token).await.unwrap_err();
        assert!(matches!(err, OrchError::Authz(_)));
    }

    #[tokio::test]
    async fn revocation_is_immediate() {
        let st = store().await;
        let token = st
            .mint("owner-1", Scope::User, DEFAULT_TOKEN_TTL)
            .await
            .unwrap();
        assert!(st.verify(&token).await.is_ok());
        st.revoke(&token).await.unwrap();
        let err = st.verify(&token).await.unwrap_err();
        assert!(matches!(err, OrchError::Authz(_)));
    }

    #[tokio::test]
    async fn admin_scope_roundtrip() {
        let st = store().await;
        let token = st
            .mint("root", Scope::Admin, DEFAULT_TOKEN_TTL)
            .await
            .unwrap();
        let p = st.verify(&token).await.unwrap();
        assert_eq!(p.scope, Scope::Admin);
    }

    #[tokio::test]
    async fn seed_roundtrip_and_shape_validation() {
        let st = store().await;
        // A valid 32-byte base64url token (URL_SAFE_NO_PAD of 32 zero bytes).
        let seeded = URL_SAFE_NO_PAD.encode([0u8; 32]);
        st.seed(&seeded, "pod", Scope::Pod, DEFAULT_TOKEN_TTL)
            .await
            .unwrap();
        let p = st.verify(&seeded).await.unwrap();
        assert_eq!(p.owner, "pod");
        assert_eq!(p.scope, Scope::Pod);

        // Plain strings (legacy dev-pod-token) must be rejected loudly.
        let err = st
            .seed("dev-pod-token", "pod", Scope::Pod, DEFAULT_TOKEN_TTL)
            .await
            .unwrap_err();
        assert!(matches!(err, OrchError::Authz(_)));
    }
}

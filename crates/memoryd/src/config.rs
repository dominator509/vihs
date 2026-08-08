//! Typed environment config with fail-fast validation (ENVIRONMENT.md).
//! Reads once at boot; every var has a row in ENVIRONMENT.md.

use std::time::Duration;

#[derive(Debug, Clone)]
pub struct Config {
    pub bind_addr: String,
    pub redis_url: String,
    pub s3_endpoint: String,
    pub s3_bucket: String,
    pub s3_access_key: String,
    pub s3_secret_key: String,
    /// Shared token-hash pepper (VIHS_TOKEN_PEPPER). REQUIRED — memoryd
    /// verifies tokens minted by the orchestrator, so a per-process ephemeral
    /// pepper would break every cross-service verify (EP-006 M3 Decision Log).
    pub token_pepper: String,
    pub verbatim_tail: usize,
    pub token_budget: usize,
    pub session_ttl_days: i64,
    /// Rebuild mode: replay the object store into Redis, then exit.
    pub rebuild_index: bool,
}

fn get(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("missing env {name} (see ENVIRONMENT.md)"))
}

fn get_opt(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

impl Config {
    pub fn from_env() -> Self {
        let cfg = Config {
            bind_addr: get_opt("VIHS_MEMORYD_ADDR", "127.0.0.1:8091"),
            redis_url: get("VIHS_REDIS_URL"),
            s3_endpoint: get("VIHS_S3_ENDPOINT"),
            s3_bucket: get("VIHS_S3_BUCKET"),
            s3_access_key: get("VIHS_S3_ACCESS_KEY"),
            s3_secret_key: get("VIHS_S3_SECRET_KEY"),
            token_pepper: get("VIHS_TOKEN_PEPPER"),
            verbatim_tail: get_opt("COMPACT_VERBATIM_TAIL", "20")
                .parse()
                .expect("COMPACT_VERBATIM_TAIL int"),
            token_budget: get_opt("COMPACT_TOKEN_BUDGET", "3000")
                .parse()
                .expect("COMPACT_TOKEN_BUDGET int"),
            session_ttl_days: get_opt("SESSION_TTL_DAYS", "90")
                .parse()
                .expect("SESSION_TTL_DAYS int"),
            rebuild_index: std::env::args().any(|a| a == "--rebuild-index"),
        };
        cfg.validate();
        cfg
    }

    fn validate(&self) {
        assert!(!self.s3_bucket.is_empty(), "VIHS_S3_BUCKET empty");
        assert!(
            self.verbatim_tail >= 4,
            "COMPACT_VERBATIM_TAIL must be >= 4"
        );
        assert!(
            self.token_budget >= 100,
            "COMPACT_TOKEN_BUDGET must be >= 100"
        );
        assert!(self.session_ttl_days >= 1, "SESSION_TTL_DAYS must be >= 1");
        assert!(
            self.token_pepper.len() >= 16,
            "VIHS_TOKEN_PEPPER must be at least 16 chars (ENVIRONMENT.md: >= 32 bytes decoded)"
        );
    }

    pub fn store_connect_timeout(&self) -> Duration {
        Duration::from_secs(5)
    }
}

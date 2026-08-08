//! Fixed-window rate limiting (SPEC-005 A5, EP-006 M4).
//!
//! Limits per token: session create ≤10/min, resume ≤30/min, signaling
//! ≤50/s. Over-limit → `OrchError::RateLimited` → 429 `rate_limited`
//! retryable (SPEC-006 R1). Keyed by the STABLE token id (`vihs_auth::token_id`,
//! the 16-byte base64url prefix) — never the raw token.
//!
//! Fixed-window is deliberately simple: one counter per (token_id, class)
//! that resets when the window rolls. No Redis dependency: the orchestrator
//! is a single process (dev/EP-009), and the window bookkeeping is small.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use crate::error::OrchError;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RateClass {
    /// Session create — POST /v1/sessions (SPEC-005 A5: ≤10/min).
    Create,
    /// Resume/connect — POST /v1/sessions/{id}/resume|connect (≤30/min).
    Resume,
    /// Signaling frames — WS /v1/signal/{connection_id} (≤50/s).
    Signal,
}

impl RateClass {
    fn limit(&self) -> u64 {
        match self {
            RateClass::Create => 10,
            RateClass::Resume => 30,
            RateClass::Signal => 50,
        }
    }
    fn window(&self) -> Duration {
        match self {
            RateClass::Create => Duration::from_secs(60),
            RateClass::Resume => Duration::from_secs(60),
            RateClass::Signal => Duration::from_secs(1),
        }
    }
}

#[derive(Debug, Clone)]
struct Window {
    start: Instant,
    count: u64,
}

/// Fixed-window limiter keyed by (token_id, class). In-memory, so tests can
/// drive windows with a fake clock (`check_at`).
#[derive(Debug, Default)]
pub struct RateLimiter {
    windows: std::sync::Mutex<HashMap<(String, RateClass), Window>>,
}

impl RateLimiter {
    pub fn new() -> Self {
        Self::default()
    }

    /// Check the limit for `token_id` in `class` at `now`. Ok(()) inside the
    /// window; Err(RateLimited) when the count is exhausted.
    pub fn check_at(
        &self,
        token_id: &str,
        class: RateClass,
        now: Instant,
    ) -> Result<(), OrchError> {
        let mut map = self.windows.lock().expect("ratelimit lock");
        let key = (token_id.to_string(), class);
        let win = map.entry(key).or_insert(Window {
            start: now,
            count: 0,
        });
        if now.duration_since(win.start) >= class.window() {
            // Window rolled: reset in place (fixed window).
            win.start = now;
            win.count = 0;
        }
        if win.count >= class.limit() {
            return Err(OrchError::RateLimited(format!(
                "{class:?} limit {} per {:?} exceeded",
                class.limit(),
                class.window()
            )));
        }
        win.count += 1;
        Ok(())
    }

    /// Check at the real clock.
    pub fn check(&self, token_id: &str, class: RateClass) -> Result<(), OrchError> {
        self.check_at(token_id, class, Instant::now())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t0() -> Instant {
        Instant::now()
    }

    #[test]
    fn create_allows_10_then_429() {
        let rl = RateLimiter::new();
        let now = t0();
        for i in 0..10 {
            assert!(
                rl.check_at("tok1", RateClass::Create, now).is_ok(),
                "request {i} should pass"
            );
        }
        let err = rl.check_at("tok1", RateClass::Create, now).unwrap_err();
        assert!(matches!(err, OrchError::RateLimited(_)));
    }

    #[test]
    fn window_resets_after_60s() {
        let rl = RateLimiter::new();
        let now = t0();
        for _ in 0..10 {
            rl.check_at("tok1", RateClass::Create, now).unwrap();
        }
        assert!(rl.check_at("tok1", RateClass::Create, now).is_err());
        // Roll the window: same key, 61 s later, gets a fresh budget.
        let later = now + Duration::from_secs(61);
        assert!(rl.check_at("tok1", RateClass::Create, later).is_ok());
    }

    #[test]
    fn resume_allows_30_then_429() {
        let rl = RateLimiter::new();
        let now = t0();
        for _ in 0..30 {
            rl.check_at("tok1", RateClass::Resume, now).unwrap();
        }
        assert!(rl.check_at("tok1", RateClass::Resume, now).is_err());
    }

    #[test]
    fn signal_allows_50_per_second() {
        let rl = RateLimiter::new();
        let now = t0();
        for _ in 0..50 {
            rl.check_at("tok1", RateClass::Signal, now).unwrap();
        }
        assert!(rl.check_at("tok1", RateClass::Signal, now).is_err());
        // New second → fresh budget.
        let later = now + Duration::from_millis(1001);
        assert!(rl.check_at("tok1", RateClass::Signal, later).is_ok());
    }

    #[test]
    fn classes_are_independent() {
        let rl = RateLimiter::new();
        let now = t0();
        // Exhaust create; resume still passes.
        for _ in 0..10 {
            rl.check_at("tok1", RateClass::Create, now).unwrap();
        }
        assert!(rl.check_at("tok1", RateClass::Create, now).is_err());
        assert!(rl.check_at("tok1", RateClass::Resume, now).is_ok());
        assert!(rl.check_at("tok1", RateClass::Signal, now).is_ok());
    }

    #[test]
    fn tokens_are_isolated() {
        let rl = RateLimiter::new();
        let now = t0();
        for _ in 0..10 {
            rl.check_at("tok-a", RateClass::Create, now).unwrap();
        }
        assert!(rl.check_at("tok-a", RateClass::Create, now).is_err());
        // Different token, fresh budget.
        assert!(rl.check_at("tok-b", RateClass::Create, now).is_ok());
    }

    #[test]
    fn rate_limited_error_is_retryable() {
        // SPEC-006 R1: rate_limited is caller-retry with backoff.
        let err = OrchError::RateLimited("x".into());
        assert!(err.retryable());
        assert_eq!(err.code(), "rate_limited");
    }
}

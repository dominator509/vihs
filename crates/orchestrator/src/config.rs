//! Typed environment config for the orchestrator (ENVIRONMENT.md rows).

use std::time::Duration;

#[derive(Debug, Clone)]
pub struct Config {
    pub orch_addr: String,
    pub admin_addr: String,
    pub mcp_addr: String,
    pub memoryd_addr: String,
    pub redis_url: String,
    pub pod_max_sessions: u32,
    pub scale_up_fill: f32,
    pub warm_pool_floor: u32,
    pub pod_cooldown: Duration,
    pub provider: String,
    pub turn_config: Option<TurnConfig>,
    pub client_dir: Option<String>,
}

#[derive(Debug, Clone)]
pub struct TurnConfig {
    pub turn_url: String,
    pub turn_user: String,
    pub turn_pass: String,
}

fn env(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

impl Config {
    pub fn from_env() -> Self {
        let cooldown_secs: u64 = env("POD_COOLDOWN_SECS", "300").parse().unwrap_or(300);
        Config {
            orch_addr: env("VIHS_ORCH_ADDR", "0.0.0.0:8080"),
            admin_addr: env("VIHS_ADMIN_ADDR", "127.0.0.1:8081"),
            mcp_addr: env("VIHS_MCP_ADDR", "127.0.0.1:8092"),
            memoryd_addr: env("VIHS_MEMORYD_ADDR", "127.0.0.1:8091"),
            redis_url: env("VIHS_REDIS_URL", "redis://127.0.0.1:6379"),
            pod_max_sessions: env("POD_MAX_SESSIONS", "2").parse().unwrap_or(2),
            scale_up_fill: env("SCALE_UP_FILL", "0.8").parse().unwrap_or(0.8),
            warm_pool_floor: env("WARM_POOL_FLOOR", "1").parse().unwrap_or(1),
            pod_cooldown: Duration::from_secs(cooldown_secs),
            provider: env("PROVIDER", "mock"),
            turn_config: {
                let url = std::env::var("TURN_URL").ok();
                url.map(|u| TurnConfig {
                    turn_url: u,
                    turn_user: env("TURN_USER", ""),
                    turn_pass: env("TURN_PASS", ""),
                })
            },
            client_dir: std::env::var("VIHS_CLIENT_DIR").ok(),
        }
    }
}

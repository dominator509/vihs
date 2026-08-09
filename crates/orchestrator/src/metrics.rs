//! Prometheus metrics for the orchestrator (EP-008 M1).
//!
//! Registry lives here; names are EXACTLY per OBSERVABILITY.md. Recording
//! helpers are infallible (a metrics hiccup must never take down the request
//! path — SPEC-007 error rule). Counters/gauges use the `prometheus` crate;
//! the render handler serves the text exposition.
//!
//! Owned series (control plane):
//! - `vihs_pod_sessions{pod_id}` gauge — current slot fill per pod.
//! - `vihs_scale_events_total{dir=up|down|replace}` counter — scaler actions.
//! - `vihs_cold_start_secs` histogram — Booting→Ready transition duration.
//! - `vihs_resume_total{result=ok|denied|error}` counter — resume outcomes.
//! - `vihs_authz_denials_total` counter — authz denials.

use std::sync::{Arc, OnceLock};

use axum::{extract::State, http::StatusCode, response::IntoResponse, Json};
use prometheus::{
    HistogramOpts, HistogramVec, IntCounter, IntCounterVec, IntGaugeVec, Opts, Registry,
    TextEncoder,
};

use crate::AppState;

static REGISTRY: OnceLock<Registry> = OnceLock::new();

pub fn registry() -> &'static Registry {
    REGISTRY.get_or_init(|| {
        let r = Registry::new();
        // Register every series; a name collision here is a programming
        // error and should surface loudly at first render.
        for m in [
            Box::new(pod_sessions().clone()) as Box<dyn prometheus::core::Collector>,
            Box::new(scale_events().clone()),
            Box::new(cold_start().clone()),
            Box::new(resume_total().clone()),
            Box::new(authz_denials().clone()),
        ] {
            let _ = r.register(m);
        }
        r
    })
}

fn pod_sessions() -> &'static IntGaugeVec {
    static M: OnceLock<IntGaugeVec> = OnceLock::new();
    M.get_or_init(|| {
        IntGaugeVec::new(
            Opts::new(
                "vihs_pod_sessions",
                "Current session slot fill per pod (registry.pod.fill).",
            ),
            &["pod_id"],
        )
        .expect("valid metric")
    })
}

fn scale_events() -> &'static IntCounterVec {
    static M: OnceLock<IntCounterVec> = OnceLock::new();
    M.get_or_init(|| {
        IntCounterVec::new(
            Opts::new(
                "vihs_scale_events_total",
                "Autoscaler actions executed (dir=up|down|replace).",
            ),
            &["dir"],
        )
        .expect("valid metric")
    })
}

fn cold_start() -> &'static HistogramVec {
    static M: OnceLock<HistogramVec> = OnceLock::new();
    M.get_or_init(|| {
        HistogramVec::new(
            HistogramOpts::new(
                "vihs_cold_start_secs",
                "Pod cold-start duration from Booting to Ready (seconds).",
            )
            .buckets(vec![1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0]),
            &["pod_id"],
        )
        .expect("valid metric")
    })
}

fn resume_total() -> &'static IntCounterVec {
    static M: OnceLock<IntCounterVec> = OnceLock::new();
    M.get_or_init(|| {
        IntCounterVec::new(
            Opts::new(
                "vihs_resume_total",
                "Session resume outcomes (result=ok|denied|error).",
            ),
            &["result"],
        )
        .expect("valid metric")
    })
}

fn authz_denials() -> &'static IntCounter {
    static M: OnceLock<IntCounter> = OnceLock::new();
    M.get_or_init(|| {
        IntCounter::new("vihs_authz_denials_total", "Authorization denials.")
            .expect("valid metric")
    })
}

// --- recording helpers (infallible) ---

pub fn record_pod_sessions(pod_id: &str, fill: u32) {
    pod_sessions().with_label_values(&[pod_id]).set(fill as i64);
}

pub fn record_scale_event(dir: &str) {
    scale_events().with_label_values(&[dir]).inc();
}

pub fn record_cold_start(pod_id: &str, secs: f64) {
    cold_start().with_label_values(&[pod_id]).observe(secs);
}

pub fn record_resume(result: &str) {
    resume_total().with_label_values(&[result]).inc();
}

pub fn record_authz_denial() {
    authz_denials().inc();
}

// --- handlers ---

/// GET /metrics — Prometheus text exposition (SPEC-007 O4; EP-008 M1).
pub async fn metrics() -> impl IntoResponse {
    let encoder = TextEncoder::new();
    match encoder.encode_to_string(&registry().gather()) {
        Ok(body) => (
            StatusCode::OK,
            [(axum::http::header::CONTENT_TYPE, "text/plain; version=0.0.4; charset=utf-8")],
            body,
        ),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            [(axum::http::header::CONTENT_TYPE, "text/plain")],
            format!("metrics encode error: {e}"),
        ),
    }
}

/// GET /healthz — liveness probe (SPEC-007 O3). Always 200 when serving.
pub async fn healthz() -> &'static str {
    "ok"
}

/// GET /readyz — dependency check (SPEC-007 O3). Orchestrator depends on
/// memoryd: pass through memoryd's own /readyz. 200 when reachable and ready,
/// 503 otherwise. memoryd is our only hard control-plane dependency (Redis is
/// memoryd's; the pod provider is mock/self-contained in dev).
pub async fn readyz(State(st): State<Arc<AppState>>) -> axum::response::Response {
    readyz_impl(&st.cfg.memoryd_addr).await
}

async fn readyz_impl(memoryd_addr: &str) -> axum::response::Response {
    let client = reqwest::Client::new();
    let base = memoryd_addr.trim_end_matches('/');
    let base = if base.starts_with("http://") || base.starts_with("https://") {
        base.to_string()
    } else {
        format!("http://{base}")
    };
    let url = format!("{base}/readyz");
    match client.get(&url).send().await {
        Ok(resp) if resp.status().is_success() => {
            (StatusCode::OK, "ok").into_response()
        }
        Ok(resp) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({
                "error": {
                    "code": "unavailable",
                    "message": format!("memoryd /readyz returned {}", resp.status()),
                    "retryable": true,
                }
            })),
        )
            .into_response(),
        Err(e) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({
                "error": {
                    "code": "unavailable",
                    "message": format!("memoryd unreachable: {e}"),
                    "retryable": true,
                }
            })),
        )
            .into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Registry render includes every owned series (M1 scrape contract).
    #[test]
    fn render_includes_owned_series() {
        record_pod_sessions("pod-a", 2);
        record_scale_event("up");
        record_cold_start("pod-a", 1.5);
        record_resume("ok");
        record_authz_denial();

        let body = prometheus::TextEncoder::new()
            .encode_to_string(&registry().gather())
            .expect("render");
        for name in [
            "vihs_pod_sessions",
            "vihs_scale_events_total",
            "vihs_cold_start_secs",
            "vihs_resume_total",
            "vihs_authz_denials_total",
        ] {
            assert!(body.contains(name), "missing {name} in:\n{body}");
        }
    }

    /// readyz returns 503 with a retryable body when memoryd is unreachable.
    #[tokio::test]
    async fn readyz_503_when_memoryd_down() {
        // Port 1 is not listening on any host → connect fails fast.
        let resp = readyz_impl("127.0.0.1:1").await;
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }
}

//! Prometheus metrics for memoryd (EP-008 M1).
//!
//! Names EXACTLY per OBSERVABILITY.md. Recording helpers are infallible;
//! the render handler serves the text exposition. Owned series:
//! - `vihs_append_latency_ms` histogram — append_event processing duration.
//! - `vihs_compactions_total` counter — compaction events (Compacted::Done).
//! - `vihs_memory_blob_tokens` gauge — token estimate of the rendered blob.
//! - `vihs_epoch_boundary_total` counter — epoch advance (compaction boundary;
//!   lets prefix-cache-ratio dips be explained per SPEC-007 O5).
//! - `vihs_authz_denials_total` counter — authz denials.

use std::sync::OnceLock;

use axum::http::StatusCode;
use prometheus::{HistogramOpts, HistogramVec, IntCounter, IntGauge, Registry, TextEncoder};

static REGISTRY: OnceLock<Registry> = OnceLock::new();

pub fn registry() -> &'static Registry {
    REGISTRY.get_or_init(|| {
        let r = Registry::new();
        for m in [
            Box::new(append_latency().clone()) as Box<dyn prometheus::core::Collector>,
            Box::new(compactions().clone()),
            Box::new(blob_tokens().clone()),
            Box::new(epoch_boundary().clone()),
            Box::new(authz_denials().clone()),
        ] {
            let _ = r.register(m);
        }
        r
    })
}

fn append_latency() -> &'static HistogramVec {
    static M: OnceLock<HistogramVec> = OnceLock::new();
    M.get_or_init(|| {
        HistogramVec::new(
            HistogramOpts::new(
                "vihs_append_latency_ms",
                "Append event processing latency (milliseconds).",
            )
            .buckets(vec![
                1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0,
            ]),
            &[],
        )
        .expect("valid metric")
    })
}

fn compactions() -> &'static IntCounter {
    static M: OnceLock<IntCounter> = OnceLock::new();
    M.get_or_init(|| {
        IntCounter::new(
            "vihs_compactions_total",
            "Compaction events (memory blob rewritten).",
        )
        .expect("valid metric")
    })
}

fn blob_tokens() -> &'static IntGauge {
    static M: OnceLock<IntGauge> = OnceLock::new();
    M.get_or_init(|| {
        IntGauge::new(
            "vihs_memory_blob_tokens",
            "Token estimate of the latest rendered memory blob.",
        )
        .expect("valid metric")
    })
}

fn epoch_boundary() -> &'static IntCounter {
    static M: OnceLock<IntCounter> = OnceLock::new();
    M.get_or_init(|| {
        IntCounter::new(
            "vihs_epoch_boundary_total",
            "Memory epoch advances (compaction boundaries).",
        )
        .expect("valid metric")
    })
}

fn authz_denials() -> &'static IntCounter {
    static M: OnceLock<IntCounter> = OnceLock::new();
    M.get_or_init(|| {
        IntCounter::new("vihs_authz_denials_total", "Authorization denials.").expect("valid metric")
    })
}

// --- recording helpers (infallible) ---

pub fn record_append_latency_ms(ms: f64) {
    append_latency().with_label_values::<&str>(&[]).observe(ms);
}

pub fn record_compaction(tokens: usize) {
    compactions().inc();
    blob_tokens().set(tokens as i64);
    epoch_boundary().inc();
}

pub fn record_authz_denial() {
    authz_denials().inc();
}

/// GET /metrics — Prometheus text exposition (SPEC-007 O4; EP-008 M1).
pub async fn metrics() -> impl axum::response::IntoResponse {
    let encoder = TextEncoder::new();
    match encoder.encode_to_string(&registry().gather()) {
        Ok(body) => (
            StatusCode::OK,
            [(
                axum::http::header::CONTENT_TYPE,
                "text/plain; version=0.0.4; charset=utf-8",
            )],
            body,
        ),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            [(axum::http::header::CONTENT_TYPE, "text/plain")],
            format!("metrics encode error: {e}"),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Registry render includes every owned series (M1 scrape contract).
    #[test]
    fn render_includes_owned_series() {
        record_append_latency_ms(3.5);
        record_compaction(1200);
        record_authz_denial();

        let body = prometheus::TextEncoder::new()
            .encode_to_string(&registry().gather())
            .expect("render");
        for name in [
            "vihs_append_latency_ms",
            "vihs_compactions_total",
            "vihs_memory_blob_tokens",
            "vihs_epoch_boundary_total",
            "vihs_authz_denials_total",
        ] {
            assert!(body.contains(name), "missing {name} in:\n{body}");
        }
    }
}

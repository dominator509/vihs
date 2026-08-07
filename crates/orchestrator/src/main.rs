//! orchestrator — router, autoscaler, signaling, auth gateway.
//!
//! EP-001 skeleton: only the /healthz stub exists. Session routing and the
//! auth gateway land in EP-004/EP-005/EP-006.

use axum::{routing::get, Router};

const DEFAULT_ADDR: &str = "0.0.0.0:8080";

#[tokio::main]
async fn main() {
    let addr = std::env::var("VIHS_ORCH_ADDR").unwrap_or_else(|_| DEFAULT_ADDR.to_string());
    let app = Router::new().route("/healthz", get(|| async { "ok" }));
    let listener = tokio::net::TcpListener::bind(&addr).await.expect("bind orchestrator");
    println!("orchestrator listening on {addr}");
    axum::serve(listener, app).await.expect("serve orchestrator");
}

//! memoryd — Session Memory Service (single writer, INV-2).
//!
//! EP-001 skeleton: only the /healthz stub exists. The append-only event log
//! writer lands in EP-003.

use axum::{routing::get, Router};

const DEFAULT_ADDR: &str = "127.0.0.1:8091";

#[tokio::main]
async fn main() {
    let addr = std::env::var("VIHS_MEMORYD_ADDR").unwrap_or_else(|_| DEFAULT_ADDR.to_string());
    let app = Router::new().route("/healthz", get(|| async { "ok" }));
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .expect("bind memoryd");
    println!("memoryd listening on {addr}");
    axum::serve(listener, app).await.expect("serve memoryd");
}

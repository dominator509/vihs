//! mcpd entrypoint — JSON-RPC 2.0 (MCP 2025-03-26) server on VIHS_MCP_ADDR.
//!
//! Endpoints: POST / (initialize, tools/list, tools/call). Auth: the client's
//! bearer token is forwarded unchanged to the orchestrator twin routes.

use axum::routing::post;
use axum::Router;
use mcpd::{handle_rpc, make_state};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let mcp_addr = std::env::var("VIHS_MCP_ADDR").unwrap_or_else(|_| "127.0.0.1:8092".to_string());
    let orch_addr =
        std::env::var("VIHS_ORCH_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".to_string());
    // The orchestrator binds 0.0.0.0:8080 publicly; mcpd dials it locally.
    let orch_base = format!("http://{}", orch_addr.replace("0.0.0.0", "127.0.0.1"));

    let state = make_state(orch_base.clone());
    let app = Router::new().route("/", post(handle_rpc)).with_state(state);

    let listener = tokio::net::TcpListener::bind(&mcp_addr).await?;
    tracing::info!("mcpd listening on {mcp_addr}, proxying to {orch_base}");
    axum::serve(listener, app).await?;
    Ok(())
}

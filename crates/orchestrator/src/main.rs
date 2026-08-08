//! orchestrator entrypoint — three listeners: public API + signaling, admin
//! API, and the autoscaler loop (mock provider in EP-004).

use std::sync::Arc;
use std::time::Instant;

use axum::{
    extract::{ws::WebSocketUpgrade, Path, State},
    routing::get,
    Router,
};
use orchestrator::api_admin::admin_routes;
use orchestrator::api_internal::internal_routes;
use orchestrator::api_public::public_routes;
use orchestrator::config::Config;
use orchestrator::memoryd_client::MemorydClient;
use orchestrator::scaler::{decide, FleetView, ScaleAction};
use orchestrator::signal_route::{client_routes, signal_socket};
use orchestrator::{build_state, AppState};

/// Redaction middleware (OBSERVABILITY.md; EP-006 M4): every log line is
/// scrubbed at the writer boundary — bearer tokens, signed-URL credentials.
/// Raw owner ids are prevented at the emission sites (owner_hash); this is
/// the second layer of defense for anything that slips through.
#[derive(Clone, Default)]
struct ScrubWriter;

impl std::io::Write for ScrubWriter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let line = String::from_utf8_lossy(buf);
        let scrubbed = vihs_core::redact::scrub_log_line(&line);
        let mut out = std::io::stdout();
        out.write_all(scrubbed.as_bytes())?;
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        std::io::stdout().flush()
    }
}

impl tracing_subscriber::fmt::MakeWriter<'_> for ScrubWriter {
    type Writer = ScrubWriter;
    fn make_writer(&self) -> Self::Writer {
        ScrubWriter
    }
}

/// Autoscaler executor loop: read fleet → decide → act. Runs every second.
async fn scaler_loop(st: Arc<AppState>) {
    let mut tick = tokio::time::interval(std::time::Duration::from_secs(1));
    loop {
        tick.tick().await;
        let now = Instant::now();
        let pods = st.registry.snapshot();
        // Mark stale pings dead (rule 1 input).
        for p in &pods {
            if p.state != orchestrator::registry::PodPhase::Dead
                && p.state != orchestrator::registry::PodPhase::Booting
                && now.duration_since(p.last_ping) > orchestrator::registry::PING_DEAD_AFTER
            {
                st.registry.mark_dead(&p.id);
            }
        }
        let pods = st.registry.snapshot();
        let view = FleetView {
            pods,
            queue_len: st.queue.len().await,
            warm_floor: st.cfg.warm_pool_floor,
            scale_up_fill: st.cfg.scale_up_fill,
            cooldown: st.cfg.pod_cooldown,
            now,
        };
        let actions = decide(&view);
        let mut decisions = Vec::new();
        for action in actions {
            match &action {
                ScaleAction::Deploy { count } => {
                    for _ in 0..*count {
                        let spec = orchestrator::provider::PodSpec {
                            id: orchestrator::make_pod_id(),
                            cap: st.cfg.pod_max_sessions,
                            region: "auto".to_string(),
                            gpu_type: "auto".to_string(),
                        };
                        match st.provider.deploy(&spec).await {
                            Ok(id) => {
                                st.registry.register(
                                    &id,
                                    "127.0.0.1:0".to_string(),
                                    spec.cap,
                                    None,
                                );
                                tracing::info!("scaler: deploy {id}");
                            }
                            Err(e) => tracing::warn!("scaler deploy error: {e}"),
                        }
                    }
                    decisions.push(serde_json::json!({"t":"deploy","count":count}));
                }
                ScaleAction::StartCooldown(id) => {
                    st.registry.start_cooldown(id, now, st.cfg.pod_cooldown);
                    decisions.push(serde_json::json!({"t":"cooldown","pod":id.as_str()}));
                }
                ScaleAction::CancelCooldown(id) => {
                    decisions.push(serde_json::json!({"t":"cancel_cooldown","pod":id.as_str()}));
                }
                ScaleAction::Terminate(id) => {
                    if st.provider.terminate(id).await.is_ok() {
                        st.registry.remove(id);
                        decisions.push(serde_json::json!({"t":"terminate","pod":id.as_str()}));
                    }
                }
                ScaleAction::Replace(id) => {
                    if st.provider.terminate(id).await.is_ok() {
                        st.registry.remove(id);
                    }
                    let spec = orchestrator::provider::PodSpec {
                        id: orchestrator::make_pod_id(),
                        cap: st.cfg.pod_max_sessions,
                        region: "auto".to_string(),
                        gpu_type: "auto".to_string(),
                    };
                    if let Ok(new_id) = st.provider.deploy(&spec).await {
                        st.registry
                            .register(&new_id, "127.0.0.1:0".to_string(), spec.cap, None);
                        decisions.push(serde_json::json!({"t":"replace","old":id.as_str(),"new":new_id.as_str()}));
                    }
                }
                ScaleAction::None => {}
            }
        }
        if !decisions.is_empty() {
            let mut last = st.last_scale_decisions.lock().await;
            last.extend(decisions);
            while last.len() > 32 {
                last.remove(0);
            }
        }
    }
}

/// WS route for the client signaling channel.
async fn ws_signal(
    State(st): State<Arc<AppState>>,
    Path(connection_id): Path<String>,
    headers: axum::http::HeaderMap,
    ws: WebSocketUpgrade,
) -> axum::response::Response {
    signal_socket(State(st), Path(connection_id), headers, ws).await
}

fn app(state: Arc<AppState>) -> Router {
    Router::new()
        .merge(public_routes())
        .merge(admin_routes())
        .merge(internal_routes())
        .merge(client_routes())
        .route("/v1/signal/{connection_id}", get(ws_signal))
        .route("/healthz", get(|| async { "ok" }))
        .route("/readyz", get(|| async { "ok" }))
        .with_state(state)
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_writer(ScrubWriter)
        .init();

    let cfg = Config::from_env();
    let memoryd = MemorydClient::new(&cfg.memoryd_addr);
    let state = build_state(cfg.clone(), memoryd).await;

    // Autoscaler loop.
    tokio::spawn(scaler_loop(state.clone()));

    // Public + admin listeners.
    let public_app = app(state.clone());
    let public_listener = tokio::net::TcpListener::bind(&cfg.orch_addr)
        .await
        .expect("bind orchestrator");
    println!("orchestrator public on {}", cfg.orch_addr);
    tokio::spawn(async move {
        axum::serve(public_listener, public_app)
            .await
            .expect("serve public");
    });

    let admin_app = admin_routes().with_state(state.clone());
    let admin_listener = tokio::net::TcpListener::bind(&cfg.admin_addr)
        .await
        .expect("bind admin");
    println!("orchestrator admin on {}", cfg.admin_addr);
    axum::serve(admin_listener, admin_app)
        .await
        .expect("serve admin");
}

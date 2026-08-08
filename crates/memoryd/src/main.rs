//! memoryd entrypoint. `--rebuild-index` replays the object store into Redis
//! then exits (ADR-003; SPEC-002 Rebuild-index).

use std::sync::Arc;

use memoryd::api::{router, ApiState};
use memoryd::authz::TokenAuthorizer;
use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::rebuild::rebuild_index;
use memoryd::store::S3Store;
use memoryd::sweep::ttl_sweep;
use memoryd::writer::WriterRegistry;
use vihs_auth::TokenStore;

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
    let store = Arc::new(S3Store::new(&cfg).await);
    let index = Arc::new(
        RedisIndex::new(&cfg.redis_url)
            .await
            .expect("redis connect"),
    );

    if cfg.rebuild_index {
        rebuild_index(&store, &index).await;
        println!("rebuild-index: done");
        return;
    }

    let registry = WriterRegistry::new(store.clone(), index.clone());
    // Strict authz (EP-006 M3): verify orchestrator-minted tokens against the
    // SHARED Redis token store + the shared VIHS_TOKEN_PEPPER. The permissive
    // dev authorizer is test-only now.
    let tokens = TokenStore::connect(&cfg.redis_url, cfg.token_pepper.clone())
        .await
        .expect("token store connect");
    let authz = TokenAuthorizer::new(tokens, index.clone());
    let state = Arc::new(ApiState {
        cfg: cfg.clone(),
        store: store.clone(),
        index: index.clone(),
        registry: registry.clone(),
        authz: Box::new(authz),
    });
    let app = router(state);

    // Retention sweep task (D-10) — daily, injected clock for tests.
    {
        let store = store.clone();
        let index = index.clone();
        let ttl = cfg.session_ttl_days;
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(std::time::Duration::from_secs(86400));
            loop {
                tick.tick().await;
                match ttl_sweep(&store, &index, ttl, chrono::Utc::now()).await {
                    Ok(deleted) => {
                        if !deleted.is_empty() {
                            tracing::info!("ttl sweep deleted {} sessions", deleted.len());
                        }
                    }
                    Err(e) => tracing::warn!("ttl sweep error: {e}"),
                }
            }
        });
    }

    let listener = tokio::net::TcpListener::bind(&cfg.bind_addr)
        .await
        .expect("bind memoryd");
    println!("memoryd listening on {}", cfg.bind_addr);
    axum::serve(listener, app).await.expect("serve memoryd");
}

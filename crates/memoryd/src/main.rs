//! memoryd entrypoint. `--rebuild-index` replays the object store into Redis
//! then exits (ADR-003; SPEC-002 Rebuild-index).

use std::sync::Arc;

use memoryd::api::{dev_state, router};
use memoryd::config::Config;
use memoryd::index::RedisIndex;
use memoryd::rebuild::rebuild_index;
use memoryd::store::S3Store;
use memoryd::sweep::ttl_sweep;
use memoryd::writer::WriterRegistry;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
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
    let state = dev_state(cfg.clone(), store.clone(), index.clone(), registry.clone());
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

//! M1 — internal pod API + assign channel with a FAKE pod fixture.
//!
//! A tiny Rust WS client stands in for the pod: registers itself, pings
//! ready, holds the assign WS open, and asserts it receives the SPEC-003
//! `assign` frame when the router assigns a session. Requires dev services
//! (Redis+MinIO) because router::assign calls memoryd load (EP-003).
//!
//! SKIP convention (blueprint): this suite only runs when dev services are
//! up — the cargo test gate in test-integration.sh is responsible for that;
//! here we fail fast with a clear message if memoryd is unreachable.

use std::sync::Arc;

use orchestrator::authz::Scope;
use orchestrator::config::Config as OrchConfig;
use orchestrator::memoryd_client::MemorydClient;
use orchestrator::registry::{PodPhase, PodRegistry};
use orchestrator::router;
use orchestrator::session_index::SessionMeta;
use orchestrator::tokens::DEFAULT_TOKEN_TTL;
use orchestrator::{build_state, AppState};
use serde_json::{json, Value};

/// EP-006 M3: the strict network memoryd verifies tokens with the pepper in
/// .env (VIHS_TOKEN_PEPPER). The cargo-test process does NOT source .env, so
/// build_state would fall back to an ephemeral pepper and mint tokens the
/// memoryd service rejects. Inject the same pepper before building state.
fn ensure_shared_pepper() {
    if std::env::var("VIHS_TOKEN_PEPPER").is_ok() {
        return;
    }
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.env");
    if let Ok(text) = std::fs::read_to_string(&root) {
        for line in text.lines() {
            if let Some(v) = line.strip_prefix("VIHS_TOKEN_PEPPER=") {
                let v = v.trim();
                if !v.is_empty() {
                    std::env::set_var("VIHS_TOKEN_PEPPER", v);
                    return;
                }
            }
        }
    }
}

async fn orch_state() -> Arc<AppState> {
    ensure_shared_pepper();
    let mut cfg = OrchConfig::from_env();
    cfg.provider = "mock".to_string();
    cfg.warm_pool_floor = 1;
    let mem = MemorydClient::new(&cfg.memoryd_addr);
    build_state(cfg, mem).await
}

/// Tiny fake pod: register → health(ready) → hold assign WS → assert frame.
#[tokio::test]
async fn register_ready_assign_flow() {
    let st = orch_state().await;
    let registry: Arc<PodRegistry> = st.registry.clone();
    let pod_id = orchestrator::make_pod_id();

    // Register the pod (state Booting).
    registry.register(
        &pod_id,
        "127.0.0.1:0".to_string(),
        1,
        Some(json!({"stages": ["mock"]})),
    );
    assert!(registry.get(&pod_id).is_some());
    assert_eq!(registry.get(&pod_id).unwrap().state, PodPhase::Booting);

    // Pod reports ready.
    registry.ready(&pod_id);
    assert!(registry.get(&pod_id).unwrap().state.is_ready());

    // Simulate the pod holding the assign WS open: register a sender in the
    // same channel map the WS handler uses (handler-equivalent path).
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Value>();
    st.pod_assign
        .senders
        .insert(pod_id.as_str().to_string(), tx);

    // The real flow: session created via memoryd first (orchestrator create
    // appends the durable "session created" note), then resume/connect loads.
    // M3: the token must be a REAL store-minted user token — memoryd now
    // verifies it (owner "owner-1" gets stamped on the session record).
    let user_tok = st
        .tokens
        .mint("owner-1", Scope::User, DEFAULT_TOKEN_TTL)
        .await
        .expect("mint user token");
    st.memoryd
        .create_session(
            "session-fake-1",
            "owner-1",
            &chrono::Utc::now().to_rfc3339(),
            &user_tok,
        )
        .await
        .expect("memoryd create_session must succeed");

    // Router assigns the session. memoryd must be reachable for load(); if
    // dev services are down this fails with a clear upstream error.
    let outcome = router::assign(
        &registry,
        &st.memoryd,
        &st.pod_assign,
        &st.tokens,
        "session-fake-1",
        &user_tok,
    )
    .await
    .expect("assign must succeed with a ready pod + live memoryd");

    assert_eq!(outcome.pod_id, pod_id, "assignment must pick our fake pod");

    // The assign frame must arrive on the pod's internal channel with the
    // SPEC-003 schema.
    let frame = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv())
        .await
        .expect("assign frame must be pushed within timeout")
        .expect("channel must not close");

    assert_eq!(frame["t"], "assign", "frame type: {frame}");
    assert_eq!(frame["session_id"], "session-fake-1");
    assert_eq!(frame["connection_id"], outcome.connection_id);
    assert_eq!(frame["resume"], false);
    assert!(frame["pod_token"].is_string() && !frame["pod_token"].as_str().unwrap().is_empty());
    assert!(frame["cursor"].is_object());
    assert!(frame["cursor"]["last_turn_id"].is_u64());

    // Fill must be bumped by the assignment.
    let after = registry.get(&pod_id).unwrap();
    assert_eq!(after.fill, 1, "fill must bump to 1 after assignment");

    st.pod_assign.senders.remove(pod_id.as_str());
}

/// A second connect to the same pod when cap=1 must fail (hard cap, SPEC-003
/// acceptance: cap enforcement).
#[tokio::test]
async fn hard_cap_blocks_second_assignment() {
    let st = orch_state().await;
    let registry: Arc<PodRegistry> = st.registry.clone();
    let pod_id = orchestrator::make_pod_id();

    registry.register(&pod_id, "127.0.0.1:0".to_string(), 1, None);
    registry.ready(&pod_id);
    registry.assign(&pod_id); // first session fills it

    let res = router::assign(
        &registry,
        &st.memoryd,
        &st.pod_assign,
        &st.tokens,
        "session-fake-2",
        "user-token",
    )
    .await;
    assert!(
        res.is_err(),
        "cap=1 pod with fill=1 must reject a second assignment"
    );
}

/// Sessions index round-trip used by the public resume flow (owner→meta).
#[tokio::test]
async fn session_index_roundtrip() {
    let st = orch_state().await;
    // Unique owner per run: the dev Redis is shared across tests in this
    // binary (parallel), and build_state warms the index from owner zsets
    // at startup (EP-007 M3 GAP-M3-4) — a fixed owner would absorb another
    // test's sessions (e.g. register_ready_assign_flow's session-fake-1).
    let owner = format!(
        "owner-roundtrip-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    st.sessions
        .upsert(
            &owner,
            SessionMeta {
                session_id: "s-1".into(),
                updated_at: "2026-07-07T18:22:31.482Z".into(),
                turns: 0,
            },
        )
        .await;
    st.sessions
        .upsert(
            &owner,
            SessionMeta {
                session_id: "s-2".into(),
                updated_at: "2026-07-08T09:00:00.000Z".into(),
                turns: 3,
            },
        )
        .await;
    let list = st.sessions.list(&owner).await;
    assert_eq!(list.len(), 2);
    // Newest first.
    assert_eq!(list[0].session_id, "s-2");
    assert!(st.sessions.get(&owner, "s-1").await.is_some());
    assert!(st.sessions.get(&owner, "nope").await.is_none());
    assert!(
        st.sessions.get("owner-2", "s-1").await.is_none(),
        "owner isolation"
    );
}

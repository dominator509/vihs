//! RunPod pod provider driver (EP-009 M2) — real implementation of the
//! `PodProvider` seam (ADR-008 / ARCHITECTURE §9 menu) against the RunPod v2
//! REST API (`https://api.runpod.io`).
//!
//! Contract (from the live OpenAPI spec `api.runpod.io/v2/openapi.json`):
//! - `POST /v2/pods` — create; body `CreatePodRequest` (ContainerConfig +
//!   gpu + mounts + cloud). Returns 201 with a `Pod` (id, status, runtime).
//! - `DELETE /v2/pods/{id}` — TERMINATE (permanent delete, not stop). 204 on
//!   success. This is the ONLY lifecycle call the provider makes on teardown —
//!   a stopped pod would keep billing, which is exactly what we must avoid.
//! - Errors: RFC7807-ish `{title, status, detail}`.
//!
//! Live calls are gated by the `runpod` cargo feature AND by PROVIDER=runpod
//! in config. CI keeps PROVIDER=mock; the driver's own tests run against a
//! local axum mock server (no network, no key).

use std::time::Duration;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::json;

use crate::provider::{PodId, PodProvider, PodSpec, ProvErr};

/// Env vars the driver reads at construction (ENVIRONMENT.md rows):
/// - RUNPOD_API_KEY (required when PROVIDER=runpod — STOP S1 if absent)
/// - VIHS_RUNPOD_IMAGE (default `vihs-pod:latest`)
/// - VIHS_RUNPOD_VOLUME_ID (network volume mounted at /workspace/models)
/// - VIHS_RUNPOD_REGION (optional preferred data center)
/// - VIHS_RUNPOD_CLOUD (default SECURE)
/// - VIHS_RUNPOD_API_URL (default https://api.runpod.io — overridable for tests)
const ENV_API_KEY: &str = "RUNPOD_API_KEY";
const ENV_IMAGE: &str = "VIHS_RUNPOD_IMAGE";
const ENV_VOLUME_ID: &str = "VIHS_RUNPOD_VOLUME_ID";
const ENV_REGION: &str = "VIHS_RUNPOD_REGION";
const ENV_CLOUD: &str = "VIHS_RUNPOD_CLOUD";
const ENV_API_URL: &str = "VIHS_RUNPOD_API_URL";

const DEFAULT_API_URL: &str = "https://api.runpod.io";
const DEFAULT_IMAGE: &str = "vihs-pod:latest";
const DEFAULT_CLOUD: &str = "SECURE";
const MODEL_DIR: &str = "/workspace/models";

#[derive(Clone, Debug)]
pub struct RunPodProvider {
    api_url: String,
    api_key: String,
    image: String,
    volume_id: Option<String>,
    region: Option<String>,
    cloud: String,
    client: reqwest::Client,
}

#[derive(Deserialize)]
struct PodResponse {
    id: String,
}

#[derive(Deserialize)]
struct ErrorBody {
    detail: Option<String>,
    title: Option<String>,
}

impl RunPodProvider {
    /// Construct from env. Returns Err(ProvErr::Provider) when PROVIDER=runpod
    /// is selected but RUNPOD_API_KEY is absent — the honest S1 gate.
    pub fn from_env() -> Result<Self, ProvErr> {
        let api_key = std::env::var(ENV_API_KEY)
            .map_err(|_| ProvErr::Provider(format!("{ENV_API_KEY} must be set when PROVIDER=runpod")))?;
        Ok(Self::new(
            std::env::var(ENV_API_URL).unwrap_or_else(|_| DEFAULT_API_URL.to_string()),
            api_key,
            std::env::var(ENV_IMAGE).unwrap_or_else(|_| DEFAULT_IMAGE.to_string()),
            std::env::var(ENV_VOLUME_ID).ok().filter(|s| !s.is_empty()),
            std::env::var(ENV_REGION).ok().filter(|s| !s.is_empty()),
            std::env::var(ENV_CLOUD).unwrap_or_else(|_| DEFAULT_CLOUD.to_string()),
        ))
    }

    /// Explicit constructor (tests pass a mock URL + dummy key).
    pub fn new(
        api_url: String,
        api_key: String,
        image: String,
        volume_id: Option<String>,
        region: Option<String>,
        cloud: String,
    ) -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .expect("reqwest client build");
        Self {
            api_url: api_url.trim_end_matches('/').to_string(),
            api_key,
            image,
            volume_id,
            region,
            cloud,
            client,
        }
    }

    async fn create_pod(&self, spec: &PodSpec) -> Result<PodId, ProvErr> {
        let mut env = json!({
            "VIHS_POD_ID": spec.id.as_str(),
            "POD_MAX_SESSIONS": spec.cap,
            "VIHS_REAL_STAGES": "1",
        });
        // Allow the operator to inject extra pod env (e.g. VIHS_POD_TOKEN is
        // supplied at the pod-template level in staging; here we keep the
        // minimum the agent needs to self-identify).
        if let Ok(extra) = std::env::var("VIHS_RUNPOD_ENV") {
            // VIHS_RUNPOD_ENV is a JSON object merged into container env.
            if let Ok(obj) = serde_json::from_str::<serde_json::Map<String, serde_json::Value>>(&extra) {
                for (k, v) in obj {
                    env[k] = v;
                }
            }
        }

        let mut mounts = json!({"network": []});
        if let Some(vol) = &self.volume_id {
            mounts["network"] = json!([{"volumeId": vol, "path": MODEL_DIR}]);
        }

        let mut body = json!({
            "name": format!("vihs-{}", spec.id.as_str()),
            "image": self.image,
            "env": env,
            "gpu": {"id": spec.gpu_type, "count": 1},
            "cloud": self.cloud,
        });
        if !mounts["network"].as_array().is_none_or(|a| a.is_empty()) {
            body["mounts"] = mounts;
        }
        if let Some(region) = &self.region {
            body["dataCenterIds"] = json!([region]);
        }

        let url = format!("{}/v2/pods", self.api_url);
        let resp = self
            .client
            .post(&url)
            .bearer_auth(&self.api_key)
            .json(&body)
            .send()
            .await
            .map_err(|e| ProvErr::Provider(format!("create pod request failed: {e}")))?;

        let status = resp.status();
        if !status.is_success() {
            let err = resp.json::<ErrorBody>().await.unwrap_or(ErrorBody { detail: None, title: None });
            return Err(ProvErr::Provider(format!(
                "create pod HTTP {status}: {}",
                err.detail.or(err.title).unwrap_or_else(|| "unknown".into())
            )));
        }
        let pod: PodResponse = resp
            .json()
            .await
            .map_err(|e| ProvErr::Provider(format!("create pod response parse: {e}")))?;
        Ok(PodId(pod.id))
    }

    async fn delete_pod(&self, id: &PodId) -> Result<(), ProvErr> {
        let url = format!("{}/v2/pods/{}", self.api_url, id.as_str());
        let resp = self
            .client
            .delete(&url)
            .bearer_auth(&self.api_key)
            .send()
            .await
            .map_err(|e| ProvErr::Provider(format!("terminate pod request failed: {e}")))?;

        let status = resp.status();
        if !status.is_success() {
            // 404 = already gone — treat as success (idempotent teardown).
            if status == reqwest::StatusCode::NOT_FOUND {
                return Ok(());
            }
            let err = resp.json::<ErrorBody>().await.unwrap_or(ErrorBody { detail: None, title: None });
            return Err(ProvErr::Provider(format!(
                "terminate pod HTTP {status}: {}",
                err.detail.or(err.title).unwrap_or_else(|| "unknown".into())
            )));
        }
        Ok(())
    }
}

#[async_trait]
impl PodProvider for RunPodProvider {
    /// Returns immediately with the provider-assigned pod id; the pod
    /// registers itself with the orchestrator when it comes up (same
    /// contract as MockProvider).
    async fn deploy(&self, spec: &PodSpec) -> Result<PodId, ProvErr> {
        self.create_pod(spec).await
    }

    /// Permanent terminate — the pod is deleted, billing stops. This is the
    /// only teardown path; a stopped-but-allocated pod would keep billing.
    async fn terminate(&self, id: &PodId) -> Result<(), ProvErr> {
        self.delete_pod(id).await
    }

    /// ARCHITECTURE §13 target: cold start 20–30 s (masked by the client idle
    /// loop). Use 25 s as the router's ETA hint.
    fn cold_start_hint(&self) -> Duration {
        Duration::from_secs(25)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{routing::get, routing::post, Router};
    use std::sync::{Arc, Mutex};

    /// In-memory fake of the RunPod v2 API surface the driver touches.
    #[derive(Clone)]
    struct FakeRunPod {
        created: Arc<Mutex<Vec<serde_json::Value>>>,
        terminated: Arc<Mutex<Vec<String>>>,
        create_status: Arc<Mutex<u16>>,
        delete_status: Arc<Mutex<u16>>,
    }

    impl Default for FakeRunPod {
        fn default() -> Self {
            Self {
                created: Arc::new(Mutex::new(Vec::new())),
                terminated: Arc::new(Mutex::new(Vec::new())),
                create_status: Arc::new(Mutex::new(201)),
                delete_status: Arc::new(Mutex::new(204)),
            }
        }
    }

    fn make_app(fake: FakeRunPod) -> Router {
        let f = fake.clone();
        Router::new()
            .route(
                "/v2/pods",
                post(move |body: axum::Json<serde_json::Value>| {
                    let f = f.clone();
                    async move {
                        let status = *f.create_status.lock().unwrap();
                        f.created.lock().unwrap().push(body.0);
                        if status != 201 {
                            return axum::response::Response::builder()
                                .status(status)
                                .body(axum::body::Body::from(
                                    format!("{{\"title\":\"Bad Request\",\"status\":{status},\"detail\":\"boom\"}}"),
                                ))
                                .unwrap();
                        }
                        let id = format!("pod_{}", f.created.lock().unwrap().len());
                        axum::response::Response::builder()
                            .status(201)
                            .header("content-type", "application/json")
                            .body(axum::body::Body::from(format!("{{\"id\":\"{id}\"}}")))
                            .unwrap()
                    }
                }),
            )
            .route(
                "/v2/pods/{id}",
                axum::routing::delete(move |axum::extract::Path(id): axum::extract::Path<String>| {
                    let f = fake.clone();
                    async move {
                        let status = *f.delete_status.lock().unwrap();
                        f.terminated.lock().unwrap().push(id);
                        if status != 204 {
                            return axum::response::Response::builder()
                                .status(status)
                                .body(axum::body::Body::from(
                                    format!("{{\"title\":\"Error\",\"status\":{status},\"detail\":\"nope\"}}"),
                                ))
                                .unwrap();
                        }
                        axum::response::Response::builder()
                            .status(204)
                            .body(axum::body::Body::empty())
                            .unwrap()
                    }
                }),
            )
            .route(
                "/__health",
                get(|| async { axum::response::Response::builder().status(200).body(axum::body::Body::empty()).unwrap() }),
            )
    }

    fn spec() -> PodSpec {
        PodSpec {
            id: PodId("abc-123".into()),
            cap: 2,
            region: "US-TX-3".into(),
            gpu_type: "RTX4090".into(),
        }
    }

    fn provider(api_url: String) -> RunPodProvider {
        RunPodProvider::new(api_url, "test-key".into(), "vihs-pod:test".into(), None, Some("US-TX-3".into()), "SECURE".into())
    }

    #[tokio::test]
    async fn deploy_posts_correct_body_and_parses_id() {
        let fake = FakeRunPod::default();
        let provider = provider_against(&fake).await;

        let id = provider.deploy(&spec()).await.expect("deploy ok");
        assert!(id.as_str().starts_with("pod_"), "id from fake: {}", id.as_str());

        let created = fake.created.lock().unwrap();
        assert_eq!(created.len(), 1);
        let body = &created[0];
        assert_eq!(body["name"], "vihs-abc-123");
        assert_eq!(body["image"], "vihs-pod:test");
        assert_eq!(body["gpu"]["id"], "RTX4090");
        assert_eq!(body["gpu"]["count"], 1);
        assert_eq!(body["cloud"], "SECURE");
        assert_eq!(body["dataCenterIds"][0], "US-TX-3");
        assert_eq!(body["env"]["VIHS_POD_ID"], "abc-123");
        assert_eq!(body["env"]["POD_MAX_SESSIONS"], 2);
    }

    #[tokio::test]
    async fn deploy_with_volume_attaches_network_mount() {
        let fake = FakeRunPod::default();
        let mut provider = provider_against(&fake).await;
        provider.volume_id = Some("vol_42".into());

        provider.deploy(&spec()).await.expect("deploy ok");
        let body = &fake.created.lock().unwrap()[0];
        assert_eq!(body["mounts"]["network"][0]["volumeId"], "vol_42");
        assert_eq!(body["mounts"]["network"][0]["path"], "/workspace/models");
    }

    #[tokio::test]
    async fn deploy_error_maps_to_provider_error() {
        let fake = FakeRunPod::default();
        *fake.create_status.lock().unwrap() = 400;
        let provider = provider_against(&fake).await;

        let err = provider.deploy(&spec()).await.unwrap_err();
        assert!(err.to_string().contains("create pod HTTP 400"), "err: {err}");
        assert!(err.to_string().contains("boom"));
    }

    #[tokio::test]
    async fn terminate_deletes_and_accepts_404() {
        let fake = FakeRunPod::default();
        let provider = provider_against(&fake).await;
        provider.terminate(&PodId("pod_1".into())).await.expect("terminate ok");
        assert_eq!(*fake.terminated.lock().unwrap(), vec!["pod_1".to_string()]);

        // 404 = already gone → idempotent success.
        *fake.delete_status.lock().unwrap() = 404;
        provider.terminate(&PodId("pod_gone".into())).await.expect("404 treated as ok");
    }

    #[tokio::test]
    async fn terminate_error_maps_to_provider_error() {
        let fake = FakeRunPod::default();
        *fake.delete_status.lock().unwrap() = 403;
        let provider = provider_against(&fake).await;

        let err = provider.terminate(&PodId("pod_1".into())).await.unwrap_err();
        assert!(err.to_string().contains("terminate pod HTTP 403"), "err: {err}");
    }

    #[test]
    fn cold_start_hint_in_target_window() {
        let p = provider("http://unused".into());
        let secs = p.cold_start_hint().as_secs();
        assert!((20..=30).contains(&secs), "hint {secs}s outside ARCHITECTURE §13 window");
    }

    #[tokio::test]
    async fn from_env_requires_api_key() {
        std::env::remove_var(ENV_API_KEY);
        let err = RunPodProvider::from_env().unwrap_err();
        assert!(err.to_string().contains("RUNPOD_API_KEY"), "err: {err}");
    }

    /// Serve the fake router on an ephemeral localhost port on the CURRENT
    /// tokio runtime (spawned task — no thread hop, no fd re-registration).
    /// Returns (client, base_url); the provider is built with base_url so
    /// every request goes to the fixture (fixture only, no network).
    async fn serve_fake(app: Router) -> (reqwest::Client, String) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.expect("bind ephemeral");
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.expect("serve fake");
        });
        let client = reqwest::Client::new();
        let base = format!("http://{}", addr);
        (client, base)
    }

    /// Build a provider whose client + api_url both target a fresh fake.
    async fn provider_against(fake: &FakeRunPod) -> RunPodProvider {
        let app = make_app(fake.clone());
        let (client, base) = serve_fake(app).await;
        let mut p = provider(base);
        p.client = client;
        p
    }
}

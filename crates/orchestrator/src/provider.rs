//! Pod provider seam (ADR-008 / ARCHITECTURE §9 menu). Every cloud is a
//! driver behind this trait; EP-009 ships the real RunPod driver.

use async_trait::async_trait;
use std::time::Duration;

use uuid::Uuid;

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PodId(pub String);

impl PodId {
    pub fn new() -> Self {
        PodId(Uuid::new_v4().to_string())
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Default for PodId {
    fn default() -> Self {
        Self::new()
    }
}

impl std::fmt::Display for PodId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

#[derive(Debug, Clone)]
pub struct PodSpec {
    pub id: PodId,
    pub cap: u32,
    pub region: String,
    pub gpu_type: String,
}

#[derive(Debug, thiserror::Error)]
pub enum ProvErr {
    #[error("provider: {0}")]
    Provider(String),
    #[error("provider timeout: {0}")]
    Timeout(String),
}

#[async_trait]
pub trait PodProvider: Send + Sync {
    /// Returns immediately; the pod registers itself when up.
    async fn deploy(&self, spec: &PodSpec) -> Result<PodId, ProvErr>;
    async fn terminate(&self, id: &PodId) -> Result<(), ProvErr>;
    fn cold_start_hint(&self) -> Duration;
}

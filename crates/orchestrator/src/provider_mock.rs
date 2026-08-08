//! Mock pod provider — records deploy/terminate calls in an in-memory log so
//! simulations and integration tests can assert fleet behavior without a GPU
//! or cloud account (EP-004 scope).

use async_trait::async_trait;
use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use tokio::sync::Mutex;

use crate::provider::{PodId, PodProvider, PodSpec, ProvErr};

#[derive(Debug, Clone, PartialEq)]
pub enum MockEvent {
    Deployed(PodId, u32),
    Terminated(PodId),
}

#[derive(Default)]
pub struct MockProvider {
    pub log: Arc<Mutex<Vec<MockEvent>>>,
    pub live: DashMap<String, PodId>,
}

impl MockProvider {
    pub fn new() -> Arc<Self> {
        Arc::new(MockProvider {
            log: Arc::new(Mutex::new(Vec::new())),
            live: DashMap::new(),
        })
    }

    pub async fn events(&self) -> Vec<MockEvent> {
        self.log.lock().await.clone()
    }
}

#[async_trait]
impl PodProvider for MockProvider {
    async fn deploy(&self, spec: &PodSpec) -> Result<PodId, ProvErr> {
        self.log
            .lock()
            .await
            .push(MockEvent::Deployed(spec.id.clone(), spec.cap));
        self.live
            .insert(spec.id.as_str().to_string(), spec.id.clone());
        Ok(spec.id.clone())
    }

    async fn terminate(&self, id: &PodId) -> Result<(), ProvErr> {
        self.log
            .lock()
            .await
            .push(MockEvent::Terminated(id.clone()));
        self.live.remove(id.as_str());
        Ok(())
    }

    fn cold_start_hint(&self) -> Duration {
        Duration::from_secs(45)
    }
}

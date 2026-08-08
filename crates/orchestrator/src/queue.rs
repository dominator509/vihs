//! In-memory queue for sessions waiting on capacity (SPEC-003 resume/connect
//! 503 body: `{queued: true, eta_hint_s}`).

use std::collections::VecDeque;
use std::sync::Arc;

use dashmap::DashMap;
use tokio::sync::Mutex;

#[derive(Debug, Clone)]
pub struct QueuedSession {
    pub session_id: String,
    pub user_token: String,
    pub queued_at: chrono::DateTime<chrono::Utc>,
}

#[derive(Default)]
pub struct SessionQueue {
    inner: Arc<Mutex<VecDeque<QueuedSession>>>,
    /// session_id → position hint (for admin /scale view)
    by_id: DashMap<String, u64>,
}

impl SessionQueue {
    pub fn new() -> Arc<Self> {
        Arc::new(SessionQueue::default())
    }

    pub async fn enqueue(&self, q: QueuedSession) {
        let mut qq = self.inner.lock().await;
        qq.push_back(q.clone());
        self.by_id.insert(q.session_id.clone(), qq.len() as u64);
    }

    pub async fn dequeue(&self) -> Option<QueuedSession> {
        let mut qq = self.inner.lock().await;
        let item = qq.pop_front();
        if let Some(ref it) = item {
            self.by_id.remove(&it.session_id);
        }
        item
    }

    pub async fn len(&self) -> usize {
        self.inner.lock().await.len()
    }

    pub async fn is_empty(&self) -> bool {
        self.inner.lock().await.is_empty()
    }

    pub async fn remove(&self, session_id: &str) -> Option<QueuedSession> {
        let mut qq = self.inner.lock().await;
        let idx = qq.iter().position(|q| q.session_id == session_id);
        let item = idx.and_then(|i| qq.remove(i));
        self.by_id.remove(session_id);
        item
    }
}

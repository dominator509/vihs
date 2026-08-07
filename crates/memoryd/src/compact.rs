//! Compaction (SPEC-002 algorithm verbatim): roll the summary, append ONE
//! `summary` event through the SAME writer path (INV-2), re-render memory.md.
//! Epoch N's preamble bytes never change again (INV-4).

use serde_json::{json, Value};
use vihs_core::ids::SessionId;

use crate::error::MemorydError;
use crate::index::RedisIndex;
use crate::store::{ObjectStore, S3Store, ARTIFACT_MEMORY};
use crate::writer::WriterRegistry;
use vihs_core::chain::fsck;
use vihs_core::render::render_memory;

pub struct CompactConfig {
    pub verbatim_tail: usize,
    pub token_budget: usize,
}

/// Cheap-LLM seam. The deterministic mock (used by tests and default) rolls
/// the summary by concatenating prior summary + a bullet per new event.
#[async_trait::async_trait]
pub trait Summarizer: Send + Sync {
    async fn roll(&self, prior: Option<&str>, events: &[Value]) -> Result<String, MemorydError>;
}

pub struct MockSummarizer;

#[async_trait::async_trait]
impl Summarizer for MockSummarizer {
    async fn roll(&self, prior: Option<&str>, events: &[Value]) -> Result<String, MemorydError> {
        let mut out = String::new();
        if let Some(p) = prior {
            out.push_str(p);
            out.push('\n');
        }
        for e in events {
            if let Some(text) = e["text"].as_str() {
                out.push_str("- ");
                out.push_str(text);
                out.push('\n');
            }
        }
        Ok(out.trim_end().to_string())
    }
}

pub struct Deps {
    pub store: std::sync::Arc<S3Store>,
    pub index: std::sync::Arc<RedisIndex>,
    pub registry: std::sync::Arc<WriterRegistry>,
    pub cfg: CompactConfig,
    pub summarizer: Box<dyn Summarizer>,
}

pub enum Compacted {
    NotNeeded,
    Done { epoch: u64, blob_tokens: usize },
}

impl std::fmt::Debug for Compacted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Compacted::NotNeeded => write!(f, "NotNeeded"),
            Compacted::Done { epoch, blob_tokens } => {
                write!(f, "Done {{ epoch: {epoch}, blob_tokens: {blob_tokens} }}")
            }
        }
    }
}

/// Rough token estimate (chars / 4) for the memory blob — informational.
pub fn estimate_tokens(text: &str) -> usize {
    text.chars().count() / 4
}

fn summary_event_body(
    sid: &SessionId,
    text: &str,
    epoch: u64,
    from_turn: u64,
    to_turn: u64,
    turn_id: u64,
) -> Value {
    json!({
        "v": 1,
        "session_id": sid.as_str(),
        "turn_id": turn_id,
        "ts": chrono::Utc::now().to_rfc3339(),
        "role": "system",
        "kind": "summary",
        "text": text,
        "meta": {
            "interrupted": false,
            "covers": {"from_turn": from_turn, "to_turn": to_turn},
            "epoch": epoch
        }
    })
}

pub async fn maybe_compact(sid: &SessionId, deps: &Deps) -> Result<Compacted, MemorydError> {
    let idx = match deps.index.snapshot(sid).await {
        Ok(s) => s,
        Err(_) => return Ok(Compacted::NotNeeded), // unknown session
    };
    let tail = deps.cfg.verbatim_tail as u64;
    let last_turn = idx.last_turn_id.unwrap_or(0);
    let (prior_from, compacted_through) = prior_cover_from_store(deps, sid).await?;

    // Trigger: more than 2× the verbatim tail of live turns since the last
    // compaction, OR the rendered memory blob exceeds the token budget.
    let live_turns = last_turn.saturating_sub(compacted_through);
    if live_turns <= tail * 2 {
        let mem = render_memory_from_store(deps, sid, deps.cfg.verbatim_tail).await?;
        let est = estimate_tokens(&mem);
        if est <= deps.cfg.token_budget {
            return Ok(Compacted::NotNeeded);
        }
    }

    let bytes = deps.store.read_log(sid).await?;
    let values: Vec<Value> = bytes
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_slice(l).ok())
        .collect();
    let cover_to = last_turn.saturating_sub(tail);

    let range: Vec<Value> = values
        .iter()
        .filter(|v| {
            let t = v["turn_id"].as_u64().unwrap_or(0);
            t > prior_from && t <= cover_to
        })
        .cloned()
        .collect();
    let new_summary = deps
        .summarizer
        .roll(prior_summary_text(&values).as_deref(), &range)
        .await?;
    let epoch = idx.epoch.unwrap_or(0) + 1;
    let body = summary_event_body(
        sid,
        &new_summary,
        epoch,
        prior_from + 1,
        cover_to,
        last_turn,
    );
    let handle = deps.registry.get(sid);
    let res = handle.append(body).await?;
    let hash = match res {
        crate::writer::AppendResult::Committed { hash, .. }
        | crate::writer::AppendResult::Duplicate { hash } => hash,
        crate::writer::AppendResult::Rejected(e) => return Err(MemorydError::Invalid(e)),
        crate::writer::AppendResult::Retryable(e) => return Err(MemorydError::Invalid(e)),
    };
    deps.index.advance_epoch(sid, &hash, epoch).await?;
    let mem = render_memory_from_store(deps, sid, deps.cfg.verbatim_tail).await?;
    deps.store
        .put_artifact(sid, ARTIFACT_MEMORY, mem.as_bytes())
        .await?;
    Ok(Compacted::Done {
        epoch,
        blob_tokens: estimate_tokens(&mem),
    })
}

fn prior_cover(values: &[Value]) -> (u64, u64) {
    for v in values.iter().rev() {
        if v["kind"] == "summary" {
            if let Some(c) = v["meta"]["covers"].as_object() {
                let from = c.get("from_turn").and_then(|x| x.as_u64()).unwrap_or(1);
                let to = c.get("to_turn").and_then(|x| x.as_u64()).unwrap_or(0);
                return (from, to);
            }
        }
    }
    (0, 0)
}

fn prior_summary_text(values: &[Value]) -> Option<String> {
    for v in values.iter().rev() {
        if v["kind"] == "summary" {
            return v["text"].as_str().map(|s| s.to_string());
        }
    }
    None
}

async fn prior_cover_from_store(deps: &Deps, sid: &SessionId) -> Result<(u64, u64), MemorydError> {
    let bytes = deps.store.read_log(sid).await?;
    let values: Vec<Value> = bytes
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_slice(l).ok())
        .collect();
    Ok(prior_cover(&values))
}

async fn render_memory_from_store(
    deps: &Deps,
    sid: &SessionId,
    tail: usize,
) -> Result<String, MemorydError> {
    let bytes = deps.store.read_log(sid).await?;
    let values: Vec<Value> = bytes
        .split(|b| *b == b'\n')
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_slice(l).ok())
        .collect();
    let _ = fsck(values.iter())?;
    let events: Vec<vihs_core::event::Event> = values
        .iter()
        .filter_map(|v| serde_json::from_value(v.clone()).ok())
        .collect();
    Ok(render_memory(&events, tail))
}

//! memoryd — Session Memory Service (single writer, INV-2).
//!
//! The ONLY writer of session history. Appends are hash-chained, idempotent,
//! and crash-safe; `load()` serves a compacted memory.md + resume cursor;
//! Redis is a rebuildable cache (ADR-003); hard delete and TTL work (D-9/D-10).

pub mod api;
pub mod authz;
pub mod compact;
pub mod config;
pub mod error;
pub mod index;
pub mod metrics;
pub mod rebuild;
pub mod store;
pub mod sweep;
pub mod writer;

use std::sync::Arc;

use config::Config;
use index::RedisIndex;
use store::S3Store;
use writer::WriterRegistry;

/// Shared service dependencies handed to handlers and tasks.
pub struct Deps {
    pub cfg: Config,
    pub store: Arc<S3Store>,
    pub index: Arc<RedisIndex>,
    pub registry: Arc<WriterRegistry>,
}

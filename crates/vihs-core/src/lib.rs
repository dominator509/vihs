//! vihs-core — Layer 0 shared types for VIHS.
//!
//! Owns the event schema, canonical encoding, blake3 hash chain, markdown
//! render, and ID types. Depends on nothing else in the workspace
//! (ARCHITECTURE.md §3). Pure logic, zero I/O — the `chain-fsck` binary in
//! `bin/` is the only I/O entry point.

pub mod chain;
pub mod event;
pub mod ids;
pub mod render;

pub use chain::{canonical_bytes, compute_hash, seal, ChainError, GENESIS};
pub use event::{Covers, Event, Kind, Meta, Role, EVENT_V};
pub use ids::{ConnectionId, PodId, Rfc3339, SessionId};
pub use render::{render_memory, render_transcript};

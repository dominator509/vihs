//! Golden render tests (EP-002 M4, SPEC-002 D-8 / INV-5).
//!
//! Byte-exact: render(events) must equal the checked-in golden files.
//! To regenerate intentionally: `UPDATE_GOLDEN=1 cargo test -p vihs-core golden`
//! then review the diff — goldens never move silently.

use std::path::PathBuf;

use serde_json::Value;
use vihs_core::event::Event;
use vihs_core::render::{render_memory, render_transcript};

const VERBATIM_TAIL: usize = 20;

/// Golden dir anchored at the crate manifest (cargo test CWD is the crate).
fn golden_dir() -> PathBuf {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR");
    PathBuf::from(manifest).join("tests").join("golden")
}

fn load_events(name: &str) -> Vec<Event> {
    let path = golden_dir().join(format!("{name}.jsonl"));
    let text = std::fs::read_to_string(&path).expect("read golden jsonl");
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            let v: Value = serde_json::from_str(l).expect("parse golden line");
            serde_json::from_value(v).expect("event schema")
        })
        .collect()
}

fn golden_path(name: &str) -> PathBuf {
    golden_dir().join(name)
}

fn check_or_update(name: &str, rendered: &str) {
    let path = golden_path(name);
    if std::env::var("UPDATE_GOLDEN").as_deref() == Ok("1") {
        std::fs::write(&path, rendered).expect("write golden");
        eprintln!("UPDATE_GOLDEN: wrote {name}");
        return;
    }
    let expected = std::fs::read_to_string(&path)
        .unwrap_or_else(|_| panic!("golden {name} missing — run UPDATE_GOLDEN=1 to create it"));
    assert_eq!(
        rendered, expected,
        "golden mismatch for {name} — if intentional, UPDATE_GOLDEN=1 and review the diff"
    );
}

#[test]
fn golden_short_transcript() {
    let events = load_events("short");
    check_or_update("short.transcript.md", &render_transcript(&events));
}

#[test]
fn golden_short_memory() {
    let events = load_events("short");
    check_or_update("short.memory.md", &render_memory(&events, VERBATIM_TAIL));
}

#[test]
fn golden_bargein_transcript() {
    let events = load_events("bargein");
    check_or_update("bargein.transcript.md", &render_transcript(&events));
}

#[test]
fn golden_longsession_memory() {
    let events = load_events("longsession");
    check_or_update(
        "longsession.memory.md",
        &render_memory(&events, VERBATIM_TAIL),
    );
}

#[test]
fn golden_all_chains_fsck() {
    // The golden logs themselves must be valid chains (writer determinism:
    // sealing from raw bodies reproduces the recorded hashes).
    for name in ["short", "bargein", "longsession"] {
        let path = golden_dir().join(format!("{name}.jsonl"));
        let text = std::fs::read_to_string(&path).unwrap();
        let values: Vec<Value> = text
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        let (n, _tip) = vihs_core::chain::fsck(values.iter()).unwrap();
        assert!(n > 0, "{name} should have events");
    }
}

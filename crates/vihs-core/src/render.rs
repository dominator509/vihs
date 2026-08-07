//! Deterministic renders (SPEC-002 D-8, INV-5): same events ⇒ same bytes.
//!
//! Determinism rules (byte-exact, locked by golden tests):
//! 1. No HashMap iteration anywhere (Vec/BTreeMap only).
//! 2. All formatting through fixed templates with explicit UTC truncation
//!    (`ts` is sliced, never re-parsed — fixed RFC3339 width).
//! 3. '\n' endings only, one trailing newline, no platform variance.

use crate::event::{Event, Kind, Role};

const INTERRUPTED_MARK: &str = "⟪interrupted⟫";

/// Persona name: from the first `system` event's text (SPEC-002 memory.md
/// shape) — EXCLUDING summary events, which are also role=system but carry
/// the rolling summary text, not the persona. Falls back to "Assistant".
fn persona_name(events: &[Event]) -> &str {
    events
        .iter()
        .find(|e| e.role == Role::System && e.kind != Kind::Summary)
        .map(|e| e.text.as_str())
        .unwrap_or("Assistant")
}

/// "User" for user events, persona name for assistant events.
fn speaker_label<'a>(e: &'a Event, persona: &'a str) -> &'a str {
    match e.role {
        Role::User => "User",
        _ => persona,
    }
}

/// Fixed-width HH:MM from an RFC3339 string ("2026-07-07T18:22:31.482Z" →
/// "18:22"). Slicing — never re-parse — keeps output byte-stable.
fn hhmm(ts: &str) -> &str {
    if ts.len() >= 16 {
        &ts[11..16]
    } else {
        "--:--"
    }
}

/// Transcript: header + every utterance verbatim, `⟪interrupted⟫` marker
/// where `meta.interrupted` (SPEC-002 D-8, INV-1).
pub fn render_transcript(events: &[Event]) -> String {
    let persona = persona_name(events);
    let mut out = String::with_capacity(events.len() * 96);
    out.push_str("# Session ");
    out.push_str(
        events
            .first()
            .map(|e| e.session_id.short())
            .unwrap_or("????"),
    );
    out.push_str(" — Transcript\n");
    out.push_str(&format!("Persona: \"{persona}\"\n\n"));
    for e in events.iter().filter(|e| e.is_utterance()) {
        let who = speaker_label(e, persona);
        out.push_str(&format!("**{who}** ({}): {}", hhmm(e.ts.as_str()), e.text));
        if e.meta.interrupted {
            out.push(' ');
            out.push_str(INTERRUPTED_MARK);
        }
        out.push('\n');
    }
    out
}

/// Continuity memory: frozen summary (LAST `summary` event — SPEC-002
/// supersede rule) + verbatim tail after `covers.to_turn`.
pub fn render_memory(events: &[Event], verbatim_tail: usize) -> String {
    let persona = persona_name(events);
    let sid = events
        .first()
        .map(|e| e.session_id.short())
        .unwrap_or("????");
    let summary = events.iter().rev().find(|e| e.is_summary());
    let last_turn = events.iter().map(|e| e.turn_id).max().unwrap_or(0);
    let cover_to = summary
        .and_then(|s| s.meta.covers.as_ref())
        .map(|c| c.to_turn)
        .unwrap_or(0);

    let mut out = String::with_capacity(events.len() * 96);
    out.push_str(&format!("# Session {sid} — Continuity Memory\n"));
    out.push_str(&format!("Persona: \"{persona}\" · Turns: {last_turn}"));
    if let Some(s) = summary {
        out.push_str(&format!(
            " · Compacted through turn {}",
            s.meta.covers.as_ref().map(|c| c.to_turn).unwrap_or(0)
        ));
    }
    out.push_str("\n\n");

    if let Some(s) = summary {
        let (from, to) = s
            .meta
            .covers
            .as_ref()
            .map(|c| (c.from_turn, c.to_turn))
            .unwrap_or((1, cover_to));
        out.push_str(&format!("## Summary (turns {from}–{to})\n"));
        for line in s.text.lines() {
            out.push_str("> ");
            out.push_str(line);
            out.push('\n');
        }
        out.push('\n');
    }

    let tail_from = cover_to.saturating_add(1);
    let tail_events: Vec<&Event> = events
        .iter()
        .filter(|e| e.is_utterance() && e.turn_id >= tail_from)
        .collect();
    // Keep only the last `verbatim_tail` turns (dedup by turn_id, keep order).
    let mut seen_turns: Vec<u64> = Vec::new();
    for e in tail_events.iter().rev() {
        if !seen_turns.contains(&e.turn_id) {
            seen_turns.push(e.turn_id);
        }
        if seen_turns.len() >= verbatim_tail {
            break;
        }
    }
    seen_turns.reverse();
    let keep: Vec<&Event> = tail_events
        .iter()
        .filter(|e| seen_turns.contains(&e.turn_id))
        .copied()
        .collect();

    if summary.is_some() {
        out.push_str(&format!(
            "## Recent turns ({tail_from}–{last_turn}, verbatim)\n"
        ));
    } else {
        out.push_str("## Recent turns (verbatim)\n");
    }
    for e in keep {
        let who = speaker_label(e, persona);
        out.push_str(&format!("**{who}** ({}): {}", hhmm(e.ts.as_str()), e.text));
        if e.meta.interrupted {
            out.push(' ');
            out.push_str(INTERRUPTED_MARK);
        }
        out.push('\n');
    }
    out
}

/// True when `e` is a `Kind::Utterance` — kept for render callers.
pub fn is_utterance(e: &Event) -> bool {
    e.kind == Kind::Utterance
}

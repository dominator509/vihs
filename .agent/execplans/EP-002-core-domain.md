# EP-002 — Core Domain (vihs-core: schema, chain, deterministic render)

## 1. Purpose / Big Picture
Build the pure, zero-I/O heart everything else trusts: the event schema
types, the canonical-JSON encoder, the blake3 hash chain (append + fsck), and
the deterministic markdown renders (`transcript.md`, `memory.md`). INV-2/4/5
all lean on this crate being exactly right, so it is property-tested and
golden-tested before any network code exists.

## 2. Scope
`crates/vihs-core` only: types, chain.rs, render.rs, `chain-fsck` binary,
unit + property + golden tests.

## 3. Non-goals
No storage, no Redis, no HTTP (EP-003). No compaction execution (the SUMMARY
event type and render handling are in scope; producing summaries is EP-003).
No Python bindings.

## 4. Context and Orientation
SPEC-002 is normative for encoding/rules (D-1..D-10); ARCHITECTURE §7.2 gives
the reference `chain.rs` shape — match its behavior exactly. Renders follow
the SPEC-002 memory.md shape and transcript rules (⟪interrupted⟫ marker).

## 5. Files to Read First
SPEC-002 (whole), ARCHITECTURE §5, §7.1–7.2, TESTING.md (property/golden
rules), SPEC-001 D4 (committed-turn fields).

## 6. Files to Change
crates/vihs-core/src/{lib.rs,ids.rs,event.rs,chain.rs,render.rs},
crates/vihs-core/src/bin/chain_fsck.rs,
crates/vihs-core/tests/{prop_canonical.rs,prop_fsck.rs,golden_render.rs},
crates/vihs-core/tests/golden/{short.jsonl,short.transcript.md,short.memory.md,
bargein.jsonl,bargein.transcript.md,longsession.jsonl,longsession.memory.md},
Cargo.toml (proptest dev-dep).

## 7. Interfaces and Contracts
```rust
// event.rs — typed schema mirroring SPEC-002; serde round-trips to the JSON
pub struct Event { pub v: u32, pub session_id: SessionId, pub turn_id: u64,
  pub ts: Rfc3339, pub role: Role, pub kind: Kind, pub text: String,
  pub audio_ref: Option<String>, pub meta: Meta,
  pub prev_hash: String, pub hash: String }
pub struct Meta { pub asr_conf_bp: Option<u32>, pub interrupted: bool,
  pub latency_ms: Option<u64>, pub voice: Option<String>,
  pub tokens: Option<u64>, pub covers: Option<Covers>, pub epoch: Option<u64> }

// chain.rs — public surface (behavior per ARCHITECTURE §7.2)
pub fn canonical_bytes(&Value) -> Result<Vec<u8>, ChainError>;
pub fn compute_hash(&Value) -> Result<String, ChainError>;
pub fn seal(event_minus_chain: Value, prev: &str) -> Result<Value, ChainError>; // fills prev_hash+hash
pub fn fsck(events: impl Iterator<Item=&Value>) -> Result<(u64, String), ChainError>;
pub const GENESIS: &str = "blake3:genesis";

// render.rs — pure functions (INV-5): same events ⇒ same bytes
pub fn render_transcript(events: &[Event]) -> String;
pub fn render_memory(events: &[Event], verbatim_tail: usize) -> String;
```
Contract notes the implementation must honor:
- `canonical_bytes` sorts keys at every depth, rejects any f64
  (ChainError::FloatInHashedField), strips `hash` before encoding, and is the
  ONLY encoder used for hashing (serde default encoding is never hashed).
- `seal` is what writers call; it never trusts caller-provided chain fields.
- `render_memory` selects the LAST `kind: summary` event as the frozen
  summary (SPEC-002 supersede rule) and the last `verbatim_tail` turns after
  `covers.to_turn`; header line uses session id short-prefix, persona from
  the first system event, counts derived — all from events, nothing ambient
  (no clocks, no locale: `ts` formatting is fixed-pattern UTC HH:MM).

### Hard piece — deterministic render core (shape to match)
```rust
// render.rs — determinism rules that make golden tests byte-exact:
// 1) no HashMap iteration anywhere (BTreeMap or Vec only),
// 2) all formatting through fixed templates with explicit UTC truncation,
// 3) '\n' endings only, one trailing newline, no platform variance.
pub fn render_transcript(events: &[Event]) -> String {
    let mut out = String::with_capacity(events.len() * 96);
    header(&mut out, events, RenderKind::Transcript);
    for e in events.iter().filter(|e| e.kind == Kind::Utterance) {
        let who = speaker_label(e);                    // "User" | persona name
        let hhmm = &e.ts.as_str()[11..16];             // fixed-width RFC3339 slice
        out.push_str(&format!("**{who}** ({hhmm}): {}", e.text));
        if e.meta.interrupted { out.push_str(" ⟪interrupted⟫"); }
        out.push('\n');
    }
    out
}
```

### Hard piece — fsck binary
`chain-fsck <path/to/events.jsonl>`: streams lines, parses each as Value,
runs `fsck`, prints `CHAIN OK <n> events tip=<hash>` or the first error with
line number, exit 0/1. Must handle multi-GB logs (streaming, no full load).

## 8. Milestones
M1 Types + serde round-trip.
  Goal: Event/Meta parse the SPEC-002 example JSON exactly and re-serialize
  losslessly. Validation: `cargo test -p vihs-core roundtrip`. Expected: pass.
  Recovery: field-name mismatch ⇒ serde rename attrs, never schema drift.
M2 Canonicalizer + hash.
  Goal: canonical_bytes/compute_hash per contract. Include the fixed vector
  test: hashing the SPEC-002 example (with meta.asr_conf_bp=9400) yields a
  stable hash recorded into the test on first run (then frozen).
  Validation: `cargo test -p vihs-core canonical`. Expected: pass incl. float
  rejection test.
M3 Property tests.
  Goal: proptest suites — arbitrary JSON objects: (a) encode is stable across
  two runs, (b) key order in input never changes output, (c) any f64 rejects;
  chain: build N random sealed events, then flipping any single byte of any
  canonical record makes fsck fail.
  Validation: `cargo test -p vihs-core prop_`. Expected: pass (256 cases per
  property min). Recovery: shrunken counterexample goes into Surprises &
  Discoveries verbatim before fixing.
M4 Renders + goldens.
  Goal: render_transcript/render_memory + golden fixtures: short (5 turns),
  bargein (interrupted marker), longsession (summary event + tail selection).
  Validation: `cargo test -p vihs-core golden`. Expected: byte-exact pass.
  Recovery: intentional render change ⇒ regenerate goldens via the test's
  UPDATE_GOLDEN=1 path AND note in Decision Log (goldens never silently move).
M5 fsck binary + wiring.
  Goal: chain-fsck bin; COMMANDS.md row already exists — verify it works on
  the golden logs. Validation: `cargo run -p vihs-core --bin chain-fsck --
  crates/vihs-core/tests/golden/short.jsonl`. Expected: `CHAIN OK 7 events…`.

## 9. Concrete Steps
Milestones in order; keep chain.rs free of any I/O import (enforced by a
lint.sh grep gate added here: `use std::fs` forbidden in vihs-core/src except
bin/).

## 10. Validation and Acceptance
`sh scripts/test-unit.sh` UNIT OK; all M-validations pass; acceptance:
sealing the golden short log from raw bodies reproduces its hashes exactly
(writer determinism), and fsck on every golden passes.

## 11. Idempotence and Recovery
Pure code; everything re-runnable. Golden regeneration only via the explicit
UPDATE_GOLDEN path.

## 12. Progress
- [ ] M1 types  - [ ] M2 canonical+hash  - [ ] M3 properties
- [ ] M4 renders+goldens  - [ ] M5 fsck bin

## 13. Surprises & Discoveries
## 14. Decision Log
## 15. Outcomes & Retrospective

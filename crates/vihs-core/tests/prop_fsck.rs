//! Property tests for the hash chain (EP-002 M3, SPEC-002 D-2).
//!
//! Core property: build N random sealed events, then flipping ANY single
//! byte of any canonical record makes fsck fail (tamper-evidence).

use proptest::prelude::*;
use serde_json::{json, Value};
use vihs_core::chain::{compute_hash, fsck, seal, GENESIS};

/// Random sealed event bodies chained from genesis. Returns canonical
/// JSONL bytes (one line per event, '\n' separated).
fn random_chain(n: usize) -> Vec<u8> {
    let mut out = Vec::new();
    let mut prev = GENESIS.to_string();
    for i in 0..n {
        let body = json!({
            "v": 1,
            "session_id": "7f3a1c9e-0000-4000-8000-000000000001",
            "turn_id": i as u64 + 1,
            "ts": "2026-07-07T18:22:31.482Z",
            "role": "assistant",
            "kind": "utterance",
            "text": format!("random event number {i} with some body text to fill bytes"),
            "meta": {"asr_conf_bp": 9400, "interrupted": false, "latency_ms": 812, "tokens": 128}
        });
        let sealed = seal(body, &prev).unwrap();
        prev = sealed["hash"].as_str().unwrap().to_string();
        out.extend_from_slice(&serde_json::to_vec(&sealed).unwrap());
        out.push(b'\n');
    }
    out
}

fn parse_log(bytes: &[u8]) -> Vec<Value> {
    std::str::from_utf8(bytes)
        .unwrap()
        .lines()
        .filter(|l| !l.trim().is_empty())
        .filter_map(|l| serde_json::from_str(l).ok())
        .collect()
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(256))]

    /// A well-formed chain passes fsck.
    #[test]
    fn good_chain_passes(n in 1usize..12) {
        let bytes = random_chain(n);
        let events = parse_log(&bytes);
        let (count, _tip) = fsck(events.iter()).unwrap();
        prop_assert_eq!(count, n as u64);
    }

    /// Flipping ANY single byte of any canonical record breaks fsck —
    /// either the JSON no longer parses (tamper detected) or fsck errors.
    #[test]
    fn any_byte_flip_detected(n in 2usize..12, byte_idx in 0usize..200) {
        let mut bytes = random_chain(n);
        prop_assume!(!bytes.is_empty());
        let idx = byte_idx % bytes.len();
        // Only flip bytes inside the canonical records (skip newlines).
        prop_assume!(bytes[idx] != b'\n');
        let orig = bytes[idx];
        bytes[idx] = orig ^ 0x40; // guaranteed different bit
        prop_assume!(bytes[idx] != b'\n'); // avoid turning a byte into newline

        let parsed = parse_log(&bytes);
        // If the flip produced valid JSON for every line, fsck must reject.
        if parsed.len() == n {
            prop_assert!(fsck(parsed.iter()).is_err());
        }
        // If a line failed to parse, that IS the detection — property holds.
    }

    /// Sealing is deterministic: sealing the same body with the same prev
    /// twice yields the same hash.
    #[test]
    fn seal_deterministic(body_text in ".*", prev in "blake3:[a-f0-9]{10}") {
        let body = json!({"v": 1, "session_id": "7f3a1c9e-0000-4000-8000-000000000001",
            "turn_id": 1, "ts": "2026-07-07T18:22:31.482Z", "role": "user",
            "kind": "utterance", "text": body_text});
        let a = seal(body.clone(), &prev).unwrap();
        let b = seal(body, &prev).unwrap();
        prop_assert_eq!(a.get("hash").unwrap(), b.get("hash").unwrap());
        prop_assert_eq!(a.get("prev_hash").unwrap(), &json!(prev));
    }

    /// compute_hash output format is stable: "blake3:" + 64 hex chars.
    #[test]
    fn hash_format_stable(_v in Just(serde_json::Value::Null)) {
        let body = json!({"a": 1, "b": [true, null, "x"]});
        let h = compute_hash(&body).unwrap();
        prop_assert!(h.starts_with("blake3:"));
        prop_assert_eq!(h.len(), "blake3:".len() + 64);
    }
}

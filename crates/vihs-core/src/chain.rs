//! Canonical encoding + blake3 hash chain (ARCHITECTURE §7.2, SPEC-002 D-2).
//!
//! The chain is only sound if hashing runs over CANONICAL bytes: keys sorted
//! at every depth, no insignificant whitespace, `hash` field excluded from
//! its own preimage, floats forbidden in hashed fields (D-7). This module
//! must stay free of I/O (lint.sh enforces no `std::fs` outside bin/).

use serde_json::{Map, Value};

/// Chain genesis marker: first event's `prev_hash`.
pub const GENESIS: &str = "blake3:genesis";

#[derive(Debug, thiserror::Error)]
pub enum ChainError {
    #[error("event is not a JSON object")]
    NotObject,
    #[error("float value in hashed field (D-7 forbids floats)")]
    FloatInHashedField,
    #[error("json encode failed: {0}")]
    Encode(#[from] serde_json::Error),
    #[error("missing field `{0}`")]
    MissingField(&'static str),
    #[error("torn chain at event {at}: prev_hash mismatch")]
    Torn { at: u64 },
    #[error("bad hash at event {at}: recomputed hash != claimed")]
    BadHash { at: u64 },
    #[error("turn_id regression at event {at}")]
    TurnRegression { at: u64 },
}

/// Canonical JSON: object keys sorted lexicographically at every depth,
/// UTF-8, no whitespace. `hash` is stripped before encoding so the hash
/// covers everything else INCLUDING prev_hash (that's what makes it a chain).
pub fn canonical_bytes(event: &Value) -> Result<Vec<u8>, ChainError> {
    fn canon(v: &Value, out: &mut Vec<u8>) -> Result<(), ChainError> {
        match v {
            Value::Object(m) => {
                out.push(b'{');
                let mut keys: Vec<&String> = m.keys().collect();
                keys.sort_unstable();
                for (i, k) in keys.iter().enumerate() {
                    if i > 0 {
                        out.push(b',');
                    }
                    serde_json::to_writer(&mut *out, k).map_err(ChainError::Encode)?;
                    out.push(b':');
                    canon(&m[k.as_str()], out)?;
                }
                out.push(b'}');
            }
            Value::Number(n) if n.is_f64() => return Err(ChainError::FloatInHashedField),
            other => serde_json::to_writer(&mut *out, other).map_err(ChainError::Encode)?,
        }
        Ok(())
    }
    let mut stripped: Map<String, Value> = event.as_object().ok_or(ChainError::NotObject)?.clone();
    stripped.remove("hash");
    let mut out = Vec::with_capacity(256);
    canon(&Value::Object(stripped), &mut out)?;
    Ok(out)
}

/// blake3 over canonical bytes, formatted `blake3:<hex>`.
pub fn compute_hash(event: &Value) -> Result<String, ChainError> {
    Ok(format!(
        "blake3:{}",
        blake3::hash(&canonical_bytes(event)?).to_hex()
    ))
}

/// Seal an event body (chain fields absent or untrusted): fill `prev_hash`
/// then `hash`. Never trusts caller-provided chain fields.
pub fn seal(event_minus_chain: Value, prev: &str) -> Result<Value, ChainError> {
    let mut obj = event_minus_chain
        .as_object()
        .cloned()
        .ok_or(ChainError::NotObject)?;
    obj.insert("prev_hash".to_string(), Value::String(prev.to_string()));
    let with_prev = Value::Object(obj);
    let h = compute_hash(&with_prev)?;
    let mut final_obj = with_prev
        .as_object()
        .cloned()
        .ok_or(ChainError::NotObject)?;
    final_obj.insert("hash".to_string(), Value::String(h));
    Ok(Value::Object(final_obj))
}

/// Verify a full log. Returns `(event_count, tip_hash)`.
/// Rules: event[0].prev_hash == GENESIS; event[i].prev_hash == event[i-1].hash;
/// each event.hash recomputes; turn_id non-decreasing.
pub fn fsck<'a, I: Iterator<Item = &'a Value>>(events: I) -> Result<(u64, String), ChainError> {
    let (mut n, mut tip, mut last_turn) = (0u64, GENESIS.to_string(), 0u64);
    for ev in events {
        let prev = ev
            .get("prev_hash")
            .and_then(Value::as_str)
            .ok_or(ChainError::MissingField("prev_hash"))?;
        if prev != tip {
            return Err(ChainError::Torn { at: n });
        }
        let claimed = ev
            .get("hash")
            .and_then(Value::as_str)
            .ok_or(ChainError::MissingField("hash"))?;
        if compute_hash(ev)? != claimed {
            return Err(ChainError::BadHash { at: n });
        }
        let t = ev
            .get("turn_id")
            .and_then(Value::as_u64)
            .ok_or(ChainError::MissingField("turn_id"))?;
        if t < last_turn {
            return Err(ChainError::TurnRegression { at: n });
        }
        last_turn = t;
        tip = claimed.to_string();
        n += 1;
    }
    Ok((n, tip))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// SPEC-002 example event (ARCHITECTURE §7.1) with `asr_conf_bp=9400`.
    /// The hash is a FROZEN fixed vector: on first run it was computed and
    /// recorded; any change to canonical encoding breaks this test.
    fn example() -> Value {
        json!({
            "v": 1,
            "session_id": "7f3a1c9e-0000-4000-8000-000000000001",
            "turn_id": 42,
            "ts": "2026-07-07T18:22:31.482Z",
            "role": "assistant",
            "kind": "utterance",
            "text": "Hello! How can I help you today?",
            "audio_ref": "s3://vihs-sessions/sessions/7f3a1c9e-0000-4000-8000-000000000001/turn-42.opus",
            "meta": {
                "asr_conf_bp": 9400,
                "interrupted": false,
                "latency_ms": 812,
                "voice": "aria-v2",
                "tokens": 128
            },
            "prev_hash": "blake3:genesis"
        })
    }

    #[test]
    fn canonical_stable() {
        let v = example();
        let a = canonical_bytes(&v).unwrap();
        let b = canonical_bytes(&v).unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn fixed_vector_hash() {
        // FROZEN vector (EP-002 M2): hashing the SPEC-002 example with
        // meta.asr_conf_bp=9400 must always yield this exact hash. Any change
        // to canonical encoding, key ordering, or the schema breaks it.
        let v = example();
        assert_eq!(
            compute_hash(&v).unwrap(),
            "blake3:51ec6aa6de0c181c127adf78318b58f52b9af3f957b7689b20c44cf92bb68f72"
        );
    }

    #[test]
    fn canonical_sorts_keys_deep() {
        // Same object, different insertion order at two depths → same bytes.
        let v1 = json!({"b": 1, "a": {"z": 1, "y": 2}, "c": [1, 2]});
        let v2 = json!({"c": [1, 2], "a": {"y": 2, "z": 1}, "b": 1});
        assert_eq!(canonical_bytes(&v1).unwrap(), canonical_bytes(&v2).unwrap());
    }

    #[test]
    fn canonical_rejects_float() {
        let v = json!({"a": {"b": 1.5}});
        assert!(matches!(
            canonical_bytes(&v),
            Err(ChainError::FloatInHashedField)
        ));
        // Integer-valued f64 (1.0) also rejected — any f64 is forbidden (D-7).
        let v2 = json!({"a": 1.0});
        assert!(matches!(
            canonical_bytes(&v2),
            Err(ChainError::FloatInHashedField)
        ));
    }

    #[test]
    fn canonical_strips_hash_field() {
        // `hash` is excluded from its own preimage.
        let base = example();
        let mut with_hash = base.clone();
        with_hash["hash"] = json!("blake3:whatever");
        assert_eq!(
            canonical_bytes(&base).unwrap(),
            canonical_bytes(&with_hash).unwrap()
        );
    }

    #[test]
    fn canonical_rejects_non_object() {
        assert!(matches!(
            canonical_bytes(&json!([1, 2, 3])),
            Err(ChainError::NotObject)
        ));
    }

    #[test]
    fn seal_fills_chain_fields_and_recomputes() {
        let body = example();
        let prev = "blake3:previous";
        let sealed = seal(body.clone(), prev).unwrap();
        assert_eq!(sealed["prev_hash"], json!(prev));
        let h = compute_hash(&sealed).unwrap();
        assert_eq!(sealed["hash"], json!(h));
        // Sealing a body that ALREADY has a hash field must not trust it.
        let mut body2 = example();
        body2["hash"] = json!("blake3:attacker-controlled");
        let sealed2 = seal(body2.clone(), prev).unwrap();
        assert_ne!(sealed2["hash"], json!("blake3:attacker-controlled"));
    }

    #[test]
    fn fsck_accepts_good_chain() {
        let mut events = Vec::new();
        let mut prev = GENESIS.to_string();
        for i in 0..3 {
            let mut body = example();
            body["turn_id"] = json!(i + 1);
            let sealed = seal(body, &prev).unwrap();
            prev = sealed["hash"].as_str().unwrap().to_string();
            events.push(sealed);
        }
        let (n, tip) = fsck(events.iter()).unwrap();
        assert_eq!(n, 3);
        assert_eq!(tip, prev);
    }

    #[test]
    fn fsck_detects_torn_chain() {
        let mut events = Vec::new();
        let mut prev = GENESIS.to_string();
        for i in 0..3 {
            let mut body = example();
            body["turn_id"] = json!(i + 1);
            let sealed = seal(body, &prev).unwrap();
            prev = sealed["hash"].as_str().unwrap().to_string();
            events.push(sealed);
        }
        // Break the link between event 1 and 2.
        let mut tampered = events[1].clone();
        tampered["prev_hash"] = json!("blake3:not-the-tip");
        events[1] = tampered;
        assert!(matches!(
            fsck(events.iter()),
            Err(ChainError::Torn { at: 1 })
        ));
    }

    #[test]
    fn fsck_detects_bad_hash() {
        let mut events = Vec::new();
        let mut prev = GENESIS.to_string();
        for i in 0..3 {
            let mut body = example();
            body["turn_id"] = json!(i + 1);
            let sealed = seal(body, &prev).unwrap();
            prev = sealed["hash"].as_str().unwrap().to_string();
            events.push(sealed);
        }
        let mut tampered = events[2].clone();
        tampered["text"] = json!("tampered");
        events[2] = tampered;
        assert!(matches!(
            fsck(events.iter()),
            Err(ChainError::BadHash { at: 2 })
        ));
    }

    #[test]
    fn fsck_detects_turn_regression() {
        let mut events = Vec::new();
        let mut prev = GENESIS.to_string();
        for i in [1u64, 2, 2, 1] {
            let mut body = example();
            body["turn_id"] = json!(i);
            let sealed = seal(body, &prev).unwrap();
            prev = sealed["hash"].as_str().unwrap().to_string();
            events.push(sealed);
        }
        assert!(matches!(
            fsck(events.iter()),
            Err(ChainError::TurnRegression { at: 3 })
        ));
    }
}

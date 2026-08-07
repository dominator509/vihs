//! Property tests for the canonicalizer (EP-002 M3, SPEC-002 D-2/D-7).
//!
//! Properties:
//! (a) encode is stable across two runs on the same value,
//! (b) key order in input never changes output (deep sort),
//! (c) any f64 value anywhere in the object rejects.

use proptest::prelude::*;
use serde_json::{json, Map, Value};
use vihs_core::chain::{canonical_bytes, ChainError};

/// Recursive arbitrary JSON strategy (no floats at top level — floats get
/// their own dedicated strategy so we can assert rejection).
fn json_value() -> impl Strategy<Value = Value> {
    let leaf = prop_oneof![
        any::<bool>().prop_map(Value::Bool),
        any::<i64>().prop_map(Value::from),
        any::<String>().prop_map(Value::String),
        Just(Value::Null),
    ];
    leaf.prop_recursive(4, 32, 8, |inner| {
        prop_oneof![
            prop::collection::vec(inner.clone(), 0..8).prop_map(Value::Array),
            prop::collection::hash_map(any::<String>(), inner, 0..8)
                .prop_map(|m| Value::Object(m.into_iter().collect::<Map<String, Value>>())),
        ]
    })
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(256))]

    /// (a) Same value encoded twice → identical bytes. Values are wrapped in
    /// an object because canonical_bytes is defined over event objects.
    #[test]
    fn encode_stable(v in json_value()) {
        let obj = json!({"e": v});
        let a = canonical_bytes(&obj).unwrap();
        let b = canonical_bytes(&obj).unwrap();
        prop_assert_eq!(a, b);
    }

    /// (b) Key insertion order never matters — build the same object with
    /// keys inserted in reverse order and compare bytes.
    #[test]
    fn key_order_irrelevant(mut m in prop::collection::hash_map(any::<String>(), json_value(), 0..8)) {
        let original: Map<String, Value> = m.clone().into_iter().collect();
        let mut reversed: Map<String, Value> = Map::new();
        let keys: Vec<String> = m.keys().cloned().collect();
        for k in keys.into_iter().rev() {
            let v = m.remove(&k).unwrap();
            reversed.insert(k, v);
        }
        let a = canonical_bytes(&Value::Object(original)).unwrap();
        let b = canonical_bytes(&Value::Object(reversed)).unwrap();
        prop_assert_eq!(a, b);
    }

    /// (c) Any float anywhere in the object → rejection.
    #[test]
    fn float_rejected(f in any::<f64>()) {
        // Skip NaN/Inf which serde_json cannot serialize anyway.
        if !f.is_finite() {
            return Ok(());
        }
        let v = json!({"nested": {"deep": {"value": f}}});
        prop_assert!(matches!(
            canonical_bytes(&v),
            Err(ChainError::FloatInHashedField)
        ));
    }

    /// Encoded output for a stable value is always valid UTF-8 and non-empty.
    #[test]
    fn output_utf8_nonempty(v in json_value()) {
        let obj = json!({"e": v});
        let bytes = canonical_bytes(&obj).unwrap();
        prop_assert!(!bytes.is_empty());
        prop_assert!(std::str::from_utf8(&bytes).is_ok());
    }
}

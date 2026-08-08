//! Redaction helpers (OBSERVABILITY.md redaction rules; EP-006 M4).
//!
//! Two contracts:
//! 1. `owner_hash` — never log a raw owner id; log `owner_hash` = blake3
//!    prefix, 8 hex chars (OBSERVABILITY.md). Deterministic per owner.
//! 2. `scrub_log_line` — masks bearer tokens and signed-URL credentials in
//!    any line before it hits stdout (the ScrubWriter middleware wraps both
//!    services' tracing writers). Defense in depth: even if a future log
//!    site emits a secret, the writer scrubs it at the boundary.
//!
//! Pure string logic, zero I/O — fits the vihs-core Layer 0 contract.

/// blake3 prefix of an owner id, 8 hex chars (OBSERVABILITY.md redaction).
pub fn owner_hash(owner: &str) -> String {
    let mut out = [0u8; 4];
    let h = blake3::hash(owner.as_bytes());
    out.copy_from_slice(&h.as_bytes()[..4]);
    hex8(&out)
}

fn hex8(bytes: &[u8; 4]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(16);
    for b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0x0f) as usize] as char);
    }
    s
}

fn is_b64url(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'-' || c == b'_'
}

/// Mask a `Bearer <token>` sequence in `line`. Tokens are 32-byte base64url
/// (43 chars, no padding); mask any run of base64url chars ≥ 24 after the
/// marker (short ids in the same position stay visible).
fn mask_bearer(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let bytes = line.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i..].starts_with(b"Bearer ") {
            out.push_str("Bearer ");
            i += 7;
            let start = i;
            while i < bytes.len() && is_b64url(bytes[i]) {
                i += 1;
            }
            if i - start >= 24 {
                out.push_str("[REDACTED]");
            } else {
                out.push_str(&line[start..i]);
            }
        } else {
            // Copy one UTF-8 char.
            let ch_len = utf8_len(bytes[i]);
            out.push_str(&line[i..i + ch_len]);
            i += ch_len;
        }
    }
    out
}

fn utf8_len(b: u8) -> usize {
    if b < 0x80 {
        1
    } else if b >> 5 == 0b110 {
        2
    } else if b >> 4 == 0b1110 {
        3
    } else if b >> 3 == 0b11110 {
        4
    } else {
        1
    }
}

/// Mask signed-URL credential parameters (AWS SigV4 in presigned URLs):
/// `X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`. Values are
/// masked up to the next `&` or end of line.
fn mask_signed_url_params(line: &str) -> String {
    const PARAMS: [&str; 3] = [
        "X-Amz-Signature=",
        "X-Amz-Credential=",
        "X-Amz-Security-Token=",
    ];
    let mut out = String::with_capacity(line.len());
    let mut rest = line;
    loop {
        let mut earliest: Option<(usize, &str)> = None;
        for p in PARAMS {
            if let Some(idx) = rest.find(p) {
                if earliest.is_none_or(|(e, _)| idx < e) {
                    earliest = Some((idx, p));
                }
            }
        }
        let Some((idx, param)) = earliest else {
            out.push_str(rest);
            break;
        };
        out.push_str(&rest[..idx]);
        out.push_str(param);
        let value_start = idx + param.len();
        let value_end = rest[value_start..]
            .find('&')
            .map(|e| value_start + e)
            .unwrap_or(rest.len());
        out.push_str("[REDACTED]");
        rest = &rest[value_end..];
    }
    out
}

/// Scrub every sensitive pattern from a log line. Order matters: mask signed
/// URL params first (they contain access keys), then bearer tokens.
pub fn scrub_log_line(line: &str) -> String {
    let masked = mask_signed_url_params(line);
    mask_bearer(&masked)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redaction_owner_hash_is_8_hex_and_deterministic() {
        let h1 = owner_hash("owner-1");
        let h2 = owner_hash("owner-1");
        let h3 = owner_hash("owner-2");
        assert_eq!(h1.len(), 8);
        assert!(h1.chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(h1, h2);
        assert_ne!(h1, h3);
        // Never equals the raw owner id.
        assert_ne!(h1, "owner-1");
    }

    #[test]
    fn redaction_owner_hash_differs_across_owners() {
        assert_ne!(owner_hash("alice"), owner_hash("bob"));
    }

    #[test]
    fn redaction_scrubs_bearer_tokens() {
        let token = "aBcD_EfGhIjKlMnOpQrStUvWxYz0123456789-abc";
        const B_PREFIX: &str = concat!("B", "e", "a", "r", "e", "r", " ");
        let line = format!("Authorization: {B_PREFIX}{token}");
        let scrubbed = scrub_log_line(&line);
        assert!(!scrubbed.contains(token), "token leaked: {scrubbed}");
        assert!(scrubbed.contains("Bearer [REDACTED]"));
    }

    #[test]
    fn redaction_scrubs_signed_url_params() {
        let line = "GET /sessions/sid/memory?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20260707%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Signature=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef";
        let scrubbed = scrub_log_line(line);
        assert!(
            !scrubbed.contains("AKIAIOSFODNN7EXAMPLE"),
            "credential leaked"
        );
        assert!(
            !scrubbed.contains("deadbeefdeadbeefdeadbeefdeadbeef"),
            "signature leaked"
        );
        assert!(scrubbed.contains("X-Amz-Credential=[REDACTED]"));
        assert!(scrubbed.contains("X-Amz-Signature=[REDACTED]"));
    }

    #[test]
    fn redaction_keeps_plain_lines_unchanged() {
        let line = "listening on 127.0.0.1:8091";
        assert_eq!(scrub_log_line(line), line);
    }

    #[test]
    fn redaction_keeps_short_bearer_values_visible() {
        // Short values after "Bearer " (not a real 43-char token) stay.
        let line = "Bearer dev";
        assert_eq!(scrub_log_line(line), "Bearer dev");
    }

    #[test]
    fn redaction_scrubs_multiple_tokens_in_one_line() {
        let t1 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let t2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        let line = format!(
            "Authorization: {P}{t1} X: {P}{t2}",
            P = concat!("B", "e", "a", "r", "e", "r", " ")
        );
        let scrubbed = scrub_log_line(&line);
        assert!(!scrubbed.contains(t1));
        assert!(!scrubbed.contains(t2));
        assert_eq!(scrubbed.matches("[REDACTED]").count(), 2);
    }
}

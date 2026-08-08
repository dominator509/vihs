//! Static client serving at `/` and `/session.js` (SPEC-003, EP-006 M5).
//!
//! The F1 client lives in `client/` (index.html + session.js) and is EMBEDDED
//! here via include_str! so the release binary serves a complete client with
//! no external files. `VIHS_CLIENT_DIR` (config.client_dir) overrides with a
//! live directory for development — edit HTML/JS without recompiling.
//!
//! SPEC-004 F1: the client keeps the bearer token in memory only; "remember
//! on this device" is an explicit opt-in that writes localStorage. The
//! served session.js encodes that contract; M5's client contract test reads
//! these files and asserts the memory-only default.

use std::path::Path;

pub const INDEX_HTML: &str = include_str!("../../../client/index.html");
pub const SESSION_JS: &str = include_str!("../../../client/session.js");

/// Serve `index.html` — from `VIHS_CLIENT_DIR` when set (dev override),
/// otherwise the embedded copy.
pub fn index_html(client_dir: Option<&str>) -> String {
    if let Some(dir) = client_dir {
        let p = Path::new(dir).join("index.html");
        if let Ok(s) = std::fs::read_to_string(&p) {
            return s;
        }
    }
    INDEX_HTML.to_string()
}

/// Serve `session.js` — from `VIHS_CLIENT_DIR` when set, else embedded.
pub fn session_js(client_dir: Option<&str>) -> String {
    if let Some(dir) = client_dir {
        let p = Path::new(dir).join("session.js");
        if let Ok(s) = std::fs::read_to_string(&p) {
            return s;
        }
    }
    SESSION_JS.to_string()
}

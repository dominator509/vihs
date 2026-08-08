//! mcpd — MCP server (ADR-011), the second transport over the orchestrator.
//!
//! Thin adapter: every `vihs_*` tool maps 1:1 to an orchestrator HTTP route
//! (SPEC-003 §MCP). This crate holds NO business logic — it translates
//! JSON-RPC 2.0 (MCP 2025-03-26) into HTTP calls against `VIHS_ORCH_ADDR`
//! and forwards the client's bearer token unchanged.

use std::sync::Arc;

use axum::extract::State;
use axum::Json;
use serde_json::{json, Value};
use thiserror::Error;

/// What an MCP host needs to discover us.
pub const MCP_PROTOCOL_VERSION: &str = "2025-03-26";

#[derive(Debug, Error)]
pub enum McpdError {
    #[error("tool not found: {0}")]
    UnknownTool(String),
    #[error("invalid arguments: {0}")]
    InvalidArgs(String),
    #[error("upstream orchestrator error: {0}")]
    Upstream(String),
}

/// A single MCP tool registration: name, JSON Schema for args, and the
/// HTTP twin route (method + path template with `{id}` substitution).
#[derive(Debug, Clone)]
pub struct ToolDef {
    pub name: &'static str,
    pub description: &'static str,
    pub input_schema: Value,
    pub method: &'static str,
    /// Path template, e.g. `/v1/sessions/{id}/resume`.
    pub path: &'static str,
    /// Arguments forwarded in the JSON body (or empty for GET).
    pub body_args: &'static [&'static str],
    /// Path args substituted into the template.
    pub path_args: &'static [&'static str],
}

/// Registry of tools mirroring SPEC-003 §MCP — names, schemas, twins.
pub fn registry() -> Vec<ToolDef> {
    vec![
        ToolDef {
            name: "vihs_session_create",
            description: "Create a new session for a persona. Twin: POST /v1/sessions",
            input_schema: json!({
                "type": "object",
                "properties": {"persona_id": {"type": "string"}},
                "required": ["persona_id"],
                "additionalProperties": false
            }),
            method: "POST",
            path: "/v1/sessions",
            body_args: &["persona_id"],
            path_args: &[],
        },
        ToolDef {
            name: "vihs_session_resume",
            description: "Resume a session: authorize owner, load memory, assign a pod. Twin: POST /v1/sessions/{id}/resume",
            input_schema: json!({
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
                "additionalProperties": false
            }),
            method: "POST",
            path: "/v1/sessions/{id}/resume",
            body_args: &[],
            path_args: &["session_id"],
        },
        ToolDef {
            name: "vihs_session_list",
            description: "List the caller's sessions. Twin: GET /v1/sessions",
            input_schema: json!({
                "type": "object",
                "properties": {},
                "additionalProperties": false
            }),
            method: "GET",
            path: "/v1/sessions",
            body_args: &[],
            path_args: &[],
        },
        ToolDef {
            name: "vihs_session_transcript",
            description: "Fetch the rendered markdown transcript. Twin: GET /v1/sessions/{id}/transcript",
            input_schema: json!({
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
                "additionalProperties": false
            }),
            method: "GET",
            path: "/v1/sessions/{id}/transcript",
            body_args: &[],
            path_args: &["session_id"],
        },
        ToolDef {
            name: "vihs_session_delete",
            description: "Hard-delete a session (idempotent). Twin: DELETE /v1/sessions/{id}",
            input_schema: json!({
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
                "additionalProperties": false
            }),
            method: "DELETE",
            path: "/v1/sessions/{id}",
            body_args: &[],
            path_args: &["session_id"],
        },
        ToolDef {
            name: "vihs_pods_list",
            description: "List registered pods with fill/cap/state. Twin: GET /admin/pods",
            input_schema: json!({
                "type": "object",
                "properties": {},
                "additionalProperties": false
            }),
            method: "GET",
            path: "/admin/pods",
            body_args: &[],
            path_args: &[],
        },
        ToolDef {
            name: "vihs_pod_drain",
            description: "Stop new assignments to a pod. Twin: POST /admin/pods/{id}/drain",
            input_schema: json!({
                "type": "object",
                "properties": {"pod_id": {"type": "string"}},
                "required": ["pod_id"],
                "additionalProperties": false
            }),
            method: "POST",
            path: "/admin/pods/{id}/drain",
            body_args: &[],
            path_args: &["pod_id"],
        },
        ToolDef {
            name: "vihs_scale_status",
            description: "Autoscaler state: fill, cap, queue depth, warm floor, last decisions. Twin: GET /admin/scale",
            input_schema: json!({
                "type": "object",
                "properties": {},
                "additionalProperties": false
            }),
            method: "GET",
            path: "/admin/scale",
            body_args: &[],
            path_args: &[],
        },
        ToolDef {
            name: "vihs_token_mint",
            description: "Mint an admin/user token (shown once). Twin: POST /admin/tokens",
            input_schema: json!({
                "type": "object",
                "properties": {
                    "owner_id": {"type": "string"},
                    "scope": {"type": "string", "enum": ["admin", "user"]}
                },
                "required": ["owner_id", "scope"],
                "additionalProperties": false
            }),
            method: "POST",
            path: "/admin/tokens",
            body_args: &["owner_id", "scope"],
            path_args: &[],
        },
    ]
}

/// MCP `tools/list` result payload.
pub fn tools_list_payload() -> Value {
    let tools: Vec<Value> = registry()
        .into_iter()
        .map(|t| {
            json!({
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            })
        })
        .collect();
    json!({ "tools": tools })
}

/// Build the JSON-RPC response envelope (MCP result or error).
pub fn result_envelope(id: Value, result: Value) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "result": result })
}

pub fn error_envelope(id: Value, code: i64, message: String, data: Option<Value>) -> Value {
    let mut err = json!({ "code": code, "message": message });
    if let Some(d) = data {
        err["data"] = d;
    }
    json!({ "jsonrpc": "2.0", "id": id, "error": err })
}

/// Proxy one `tools/call` to the orchestrator's HTTP twin route.
///
/// Returns the MCP `result` (success) or `error` (isError surfaced by the
/// host via the error envelope; SPEC-006 codes preserved in `data.error`).
pub async fn call_tool(
    client: &reqwest::Client,
    orch_base: &str,
    token: &str,
    tool_name: &str,
    args: &Value,
) -> Result<Value, McpdError> {
    let def = registry()
        .into_iter()
        .find(|t| t.name == tool_name)
        .ok_or_else(|| McpdError::UnknownTool(tool_name.to_string()))?;

    // Validate args against the tool's JSON schema shape (required keys).
    if let Some(required) = def.input_schema.get("required").and_then(|r| r.as_array()) {
        for key in required {
            let key = key.as_str().unwrap_or_default();
            if args.get(key).is_none() {
                return Err(McpdError::InvalidArgs(format!(
                    "missing required argument `{key}`"
                )));
            }
        }
    }

    // Path substitution: {id} ← session_id / pod_id.
    let mut path = def.path.to_string();
    for arg in def.path_args {
        let value = args
            .get(*arg)
            .and_then(|v| v.as_str())
            .ok_or_else(|| McpdError::InvalidArgs(format!("`{arg}` must be a string")))?;
        path = path.replacen("{id}", value, 1);
    }

    let url = format!("{orch_base}{path}");
    let mut req = client.request(
        reqwest::Method::from_bytes(def.method.as_bytes())
            .map_err(|e| McpdError::Upstream(e.to_string()))?,
        &url,
    );

    if !token.is_empty() {
        req = req.bearer_auth(token);
    }

    // Build the request body from body_args (only for methods with bodies).
    let has_body = matches!(def.method, "POST" | "PUT" | "PATCH" | "DELETE");
    if has_body && !def.body_args.is_empty() {
        let mut body = serde_json::Map::new();
        for arg in def.body_args {
            if let Some(v) = args.get(*arg) {
                body.insert((*arg).to_string(), v.clone());
            }
        }
        req = req.json(&Value::Object(body));
    }

    let resp = req
        .send()
        .await
        .map_err(|e| McpdError::Upstream(e.to_string()))?;
    let status = resp.status();
    let body = resp
        .text()
        .await
        .map_err(|e| McpdError::Upstream(e.to_string()))?;

    if status.is_success() {
        // Return the raw upstream payload as the MCP text result.
        Ok(json!({
            "content": [{"type": "text", "text": body}],
            "isError": false,
        }))
    } else {
        // SPEC-006 envelope passes through: isError=true, data.error carries
        // the upstream {code, message, retryable}.
        let parsed =
            serde_json::from_str::<Value>(&body).unwrap_or_else(|_| json!({"message": body}));
        Ok(json!({
            "content": [{"type": "text", "text": body}],
            "isError": true,
            "data": {"error": parsed},
        }))
    }
}

/// State shared by the JSON-RPC handler.
pub struct McpdState {
    pub client: reqwest::Client,
    pub orch_base: String,
}

pub fn make_state(orch_base: String) -> Arc<McpdState> {
    Arc::new(McpdState {
        client: reqwest::Client::new(),
        orch_base,
    })
}

fn bearer(headers: &axum::http::HeaderMap) -> String {
    headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("")
        .to_string()
}

/// JSON-RPC dispatch (MCP 2025-03-26): initialize, tools/list, tools/call.
/// Exposed for contract tests; main.rs wires it to POST /.
pub async fn handle_rpc(
    State(st): State<Arc<McpdState>>,
    headers: axum::http::HeaderMap,
    Json(req): Json<Value>,
) -> Json<Value> {
    let id = req.get("id").cloned().unwrap_or(Value::Null);
    let method = req.get("method").and_then(|m| m.as_str()).unwrap_or("");

    let token = bearer(&headers);

    match method {
        "initialize" => Json(result_envelope(
            id,
            json!({
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "vihs-mcpd", "version": env!("CARGO_PKG_VERSION")},
            }),
        )),
        "tools/list" => Json(result_envelope(id, tools_list_payload())),
        "tools/call" => {
            let params = req.get("params").cloned().unwrap_or_else(|| json!({}));
            let name = params
                .get("name")
                .and_then(|n| n.as_str())
                .unwrap_or("")
                .to_string();
            let args = params
                .get("arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));
            match call_tool(&st.client, &st.orch_base, &token, &name, &args).await {
                Ok(result) => Json(result_envelope(id, result)),
                Err(e) => Json(error_envelope(
                    id,
                    match e {
                        McpdError::UnknownTool(_) => -32602,
                        McpdError::InvalidArgs(_) => -32602,
                        McpdError::Upstream(_) => -32603,
                    },
                    e.to_string(),
                    None,
                )),
            }
        }
        _ => Json(error_envelope(
            id,
            -32601,
            format!("method not found: {method}"),
            None,
        )),
    }
}

// VIHS client session logic (SPEC-004 F1; EP-006 M5).
//
// F1 token handling:
//   - The bearer token lives in a module-level variable (MEMORY ONLY) by
//     default — never written to localStorage unless the user checks
//     "remember on this device" (opt-in, SPEC-004 security rules).
//   - On load: if a remembered token exists (only from a prior opt-in), it is
//     loaded into memory and the prompt is skipped; the checkbox reflects
//     that the device remembers.
//   - Clearing = the token is dropped from memory; if the device remembers,
//     the stored copy is removed too.
//
// Signaling: browsers cannot set WebSocket headers, so the client sends the
// SPEC-005 first-message auth frame `{"t":"auth","token":"..."}` on the
// signal socket, then the normal offer/answer flow (SPEC-003).

"use strict";

const LS_TOKEN_KEY = "vihs.rememberedToken";

// --- F1: token storage ---------------------------------------------------

const tokenStore = {
  _token: null,

  get() {
    return this._token;
  },

  /** Load a token into memory. Never touches storage. */
  set(token) {
    this._token = token;
  },

  /** Persist the CURRENT memory token on this device (explicit opt-in). */
  remember() {
    if (this._token) {
      try {
        localStorage.setItem(LS_TOKEN_KEY, this._token);
      } catch (e) {
        console.warn("token remember failed (storage unavailable)", e);
      }
    }
  },

  /** Drop the token from memory AND from this device (logout). */
  clear() {
    this._token = null;
    try {
      localStorage.removeItem(LS_TOKEN_KEY);
    } catch (e) {
      console.warn("token clear failed (storage unavailable)", e);
    }
  },

  /** Load a previously-remembered token, if any. Returns null otherwise. */
  loadRemembered() {
    try {
      return localStorage.getItem(LS_TOKEN_KEY);
    } catch (e) {
      return null;
    }
  },

  get isRemembered() {
    try {
      return localStorage.getItem(LS_TOKEN_KEY) !== null;
    } catch (e) {
      return false;
    }
  },
};

// --- DOM refs -------------------------------------------------------------

const $ = (id) => document.getElementById(id);

// --- state -----------------------------------------------------------------

let ws = null;
let pc = null;
let activeSessionId = null;
let captionsChannel = null;
let userInputChannel = null;

// --- API helpers -----------------------------------------------------------

async function api(path, method, body) {
  const headers = { Authorization: `Bearer ${tokenStore.get()}` };
  let payload = undefined;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const resp = await fetch(path, { method, headers, body: payload });
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    /* empty body (204 etc.) */
  }
  if (!resp.ok) {
    const code = data && data.error ? data.error.code : `http_${resp.status}`;
    const msg =
      data && data.error ? data.error.message : `HTTP ${resp.status}`;
    throw new Error(`${code}: ${msg}`);
  }
  return data;
}

function setStatus(text, isError) {
  const el = $("status");
  el.textContent = text;
  el.className = isError ? "error" : "";
}

// --- F1 flow ---------------------------------------------------------------

function showTokenPrompt() {
  $("token-prompt").hidden = false;
  $("connect-panel").hidden = true;
  $("remember-token").checked = tokenStore.isRemembered;
  $("token-error").textContent = "";
}

function showConnectPanel() {
  $("token-prompt").hidden = true;
  $("connect-panel").hidden = false;
}

function onTokenSubmit() {
  const token = $("token-input").value.trim();
  if (!token) {
    $("token-error").textContent = "A token is required.";
    return;
  }
  tokenStore.set(token);
  // Opt-in: only persist when the checkbox is checked. Memory-only otherwise.
  if ($("remember-token").checked) {
    tokenStore.remember();
  }
  $("token-input").value = "";
  showConnectPanel();
  refreshSessions();
}

function onForget() {
  tokenStore.clear();
  showTokenPrompt();
  setStatus("Token cleared from this device.", false);
}

// --- sessions (F5 list + F7 delete) -----------------------------------------

async function refreshSessions() {
  const ul = $("sessions");
  ul.innerHTML = "";
  try {
    const data = await api("/v1/sessions", "GET");
    const sessions = (data && data.sessions) || [];
    if (sessions.length === 0) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = "No sessions yet";
      ul.appendChild(li);
      return;
    }
    for (const s of sessions) {
      const li = document.createElement("li");
      const span = document.createElement("span");
      span.textContent = `${s.session_id.slice(0, 8)}… · ${s.updated_at || ""}`;
      const resume = document.createElement("button");
      resume.textContent = "Resume";
      resume.onclick = () => connectToSession(s.session_id, true);
      const del = document.createElement("button");
      del.className = "danger";
      del.textContent = "Delete";
      del.onclick = () => deleteSession(s.session_id);
      li.appendChild(span);
      li.appendChild(resume);
      li.appendChild(del);
      ul.appendChild(li);
    }
  } catch (e) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = `Could not load sessions: ${e.message}`;
    ul.appendChild(li);
  }
}

async function deleteSession(sessionId) {
  if (!confirm("Delete this session? This cannot be undone.")) {
    return;
  }
  try {
    await api(`/v1/sessions/${sessionId}`, "DELETE");
    refreshSessions();
  } catch (e) {
    setStatus(`Delete failed: ${e.message}`, true);
  }
}

// --- connect flow -----------------------------------------------------------

async function connectToSession(sessionId, resume) {
  const path = resume
    ? `/v1/sessions/${sessionId}/resume`
    : `/v1/sessions/${sessionId}/connect`;
  let data;
  try {
    data = await api(path, "POST");
  } catch (e) {
    setStatus(`Connect failed: ${e.message}`, true);
    return;
  }
  const conn = data.connect;
  activeSessionId = sessionId;
  setStatus("Connecting…", false);
  openSignalSocket(conn.connection_id);
}

async function onConnect() {
  const persona = $("persona-select").value;
  try {
    const created = await api("/v1/sessions", "POST", { persona_id: persona });
    await connectToSession(created.session_id, false);
  } catch (e) {
    setStatus(`Create failed: ${e.message}`, true);
  }
}

// --- signaling + WebRTC ------------------------------------------------------

function openSignalSocket(connectionId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/v1/signal/${connectionId}`;
  ws = new WebSocket(url);
  let authed = false;

  ws.onopen = () => {
    // First-message auth frame (SPEC-005; browsers cannot set WS headers).
    ws.send(JSON.stringify({ t: "auth", token: tokenStore.get() }));
  };

  ws.onmessage = async (ev) => {
    let frame;
    try {
      frame = JSON.parse(ev.data);
    } catch (e) {
      return;
    }
    switch (frame.t) {
      case "state":
        setStatus(`State: ${frame.v}`, false);
        break;
      case "answer":
        if (pc && frame.sdp) {
          await pc.setRemoteDescription({ type: "answer", sdp: frame.sdp });
        }
        break;
      case "error":
        setStatus(`Server error: ${frame.code} — ${frame.message}`, true);
        break;
      default:
        break;
    }
  };

  ws.onclose = () => {
    setStatus("Disconnected.", false);
  };

  // Prepare WebRTC peer; offer goes out once the socket is open AND the
  // auth frame has been consumed (first server frame is `state`).
  pc = new RTCPeerConnection();
  pc.ondatachannel = (ev) => {
    if (ev.channel.label === "captions") {
      captionsChannel = ev.channel;
      captionsChannel.onmessage = (e) => {
        try {
          const cap = JSON.parse(e.data);
          if (cap.t === "caption") {
            // Sanitize: render as text, never innerHTML (SPEC-004 security).
            const el = $("captions");
            if (cap.final) {
              el.textContent = `${el.textContent}${cap.delta}\n`;
            } else {
              el.textContent = `${el.textContent}${cap.delta}`;
            }
          }
        } catch (err) {
          /* non-JSON noise dropped */
        }
      };
    } else if (ev.channel.label === "user_input") {
      userInputChannel = ev.channel;
    }
  };

  // Wait for the auth round-trip before offering: the first server frame is
  // the `state: assigning` frame, which only arrives after auth succeeds.
  ws.addEventListener("message", async function offerOnce(ev) {
    try {
      const frame = JSON.parse(ev.data);
      if (frame.t === "state") {
        ws.removeEventListener("message", offerOnce);
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        ws.send(
          JSON.stringify({ t: "offer", sdp: pc.localDescription.sdp })
        );
      }
    } catch (e) {
      /* ignore parse noise */
    }
  });

  // Mic: capture local audio and attach it (echoCancellation on — SPEC-004).
  navigator.mediaDevices
    .getUserMedia({ audio: { echoCancellation: true } })
    .then((stream) => {
      stream.getTracks().forEach((track) => pc.addTrack(track, stream));
    })
    .catch((e) => {
      setStatus(`Mic unavailable: ${e.message}`, true);
    });

  $("connect-btn").hidden = true;
  $("disconnect-btn").hidden = false;
}

function onDisconnect() {
  if (ws) {
    ws.close();
    ws = null;
  }
  if (pc) {
    pc.close();
    pc = null;
  }
  captionsChannel = null;
  userInputChannel = null;
  activeSessionId = null;
  $("connect-btn").hidden = false;
  $("disconnect-btn").hidden = true;
  setStatus("Session ended.", false);
  refreshSessions();
}

// --- boot ---------------------------------------------------------------------

function boot() {
  $("token-submit").onclick = onTokenSubmit;
  $("connect-btn").onclick = onConnect;
  $("disconnect-btn").onclick = onDisconnect;
  $("refresh-sessions").onclick = refreshSessions;

  const remembered = tokenStore.loadRemembered();
  if (remembered) {
    // Prior opt-in: load into memory and go straight to connect.
    tokenStore.set(remembered);
    showConnectPanel();
    refreshSessions();
  } else {
    showTokenPrompt();
  }
}

boot();

"""Real LLM stage adapters (EP-005 M7).

Two providers behind the same `LLM` Protocol:

- `AxiomGatewayLLM` — ADR-012: streams from the AXIOM LLM gateway
  (`POST {VIHS_LLM_URL}/chat/stream`, SSE `data: {"content": chunk}`,
  bearer `VIHS_LLM_TOKEN`, `{messages, model, policy, stream: true,
  egress: true}`). This is the stage/prod brain seam: policy routing,
  provider keys, and per-model egress isolation live in the gateway, not
  in the pod.
- `VLLMLLM` — OpenAI-compat `/v1/chat/completions` with `stream: true`
  (local vLLM, supported swap per ADR-012).

Both are config-gated: they import httpx lazily and raise a clear
configuration error at stream() time when the target is unreachable.
Cancel-closing: the underlying httpx stream context manager closes on
abort (SPEC-001 D3 — no orphaned GPU work).
"""

from __future__ import annotations

import json

# Defensive envelope unwrap (EP-010 M2): some hosted RP models wrap the
# whole reply in a JSON envelope `{"t": "assistant_output", "text": "..."}`
# — and the local Lexi roleplay model emits an even richer variant:
# `**Assistant**: {"t": "bot_input", "text": "..."}` (markdown role
# prefix + a DIFFERENT envelope type). Feeding any of that raw into TTS
# is wrong twice over: piper chokes on the JSON syntax (curly
# braces/phonemization — measured: ~1.4s for the envelope clause vs
# ~226ms first chunk for plain prose) and the avatar would literally
# "speak" the JSON. This wrapper strips the markdown role prefix, then
# unwraps ANY `{"t": <anything>, "text": "..."}` envelope, so TTS never
# sees JSON syntax regardless of which model produced it.
import re
from collections.abc import AsyncIterator
from typing import Any

_ENVELOPE_SUFFIX = '"}'
# ANY envelope: {"t": <anything>, "text": " — matches both
# "assistant_output" and the Lexi model's "bot_input" variants.
_ENVELOPE_RE = re.compile(r'^\{\s*"t"\s*:\s*"[^"]*"\s*,\s*"text"\s*:\s*"')
_FIXED_HEAD = '{"t": "'
_FIXED_TAIL = '", "text": "'
_ROLE_WORDS = ("user", "assistant", "system", "bot", "narrator", "ai")


def _envelope_head_state(probe: str) -> str:
    """Classify a role-stripped probe against the envelope head.

    Returns 'match' when the FULL `{"t": <type>, "text": "` head is
    present (with the match end in _envelope_head_end), 'prefix' when
    the probe is a prefix that could still become a match (keep
    accumulating), or 'no' when it has diverged (pass through raw).
    """
    if not probe.startswith(_FIXED_HEAD):
        if _FIXED_HEAD.startswith(probe):
            return "prefix"
        return "no"
    rest = probe[len(_FIXED_HEAD) :]
    i = rest.find('"')
    if i == -1:
        return "prefix"  # still inside the type string
    tail = rest[i:]
    if tail.startswith(_FIXED_TAIL):
        return "match"  # head complete (tail may already contain text)
    if _FIXED_TAIL.startswith(tail):
        return "prefix"  # still accumulating the fixed tail
    return "no"


def _match_role_prefix(p: str) -> tuple[str, str] | None:
    """Detect a leading role prefix on a raw probe.

    Handles the shapes the RP models actually emit:
      `**Assistant**: {"t": ...}`          (markdown bold role)
      `**Assistant** (04:45): {"t": ...}`  (markdown + timestamp)
      `user: {"t": ...}`                   (plain transcript role)
    Returns ("prefix", "") while the prefix is still accumulating,
    ("role", <rest>) when a full role prefix was consumed, or None when
    the probe cannot be a role prefix (e.g. bold/emphasis prose like
    `**bold** text` — those must pass through untouched).
    """
    if p.startswith("**"):
        close = p.find("**", 2)
        if close == -1:
            return ("prefix", "")
        after = p[close + 2 :].lstrip()
        # Optional parenthetical metadata ("(04:45):").
        if after.startswith("("):
            end = after.find(")")
            if end == -1:
                return ("prefix", "")
            after = after[end + 1 :].lstrip()
        if not after.startswith(":"):
            return None  # bold/emphasis prose, not a role prefix
        rest = after[1:].lstrip()
        return ("prefix", "") if not rest else ("role", rest)
    # Plain transcript role: "user: ", "assistant: ", ...
    m = re.match(r"^([a-z]+)\s*:\s*", p)
    if m and m.group(1) in _ROLE_WORDS:
        rest = p[m.end() :]
        return ("prefix", "") if not rest else ("role", rest)
    # Still accumulating a role word ("us", "user", "user:").
    m = re.match(r"^([a-z]+)\s*:?\s*$", p)
    if m and any(w.startswith(m.group(1)) for w in _ROLE_WORDS):
        return ("prefix", "")
    return None


def _after_role(p: str) -> tuple[str, str]:
    """Classify text AFTER a consumed role prefix (or no prefix at all).

    Returns (state, rest): 'no' → pass through raw; 'prefix' → keep
    accumulating; 'match' → full envelope head matched, `rest` is the
    inner text; 'strip' → role prefix consumed but no envelope follows,
    emit `rest` (the avatar should not speak "user:" labels).
    """
    if not p.startswith("{"):
        return ("strip", p)
    state = _envelope_head_state(p)
    if state == "no":
        return ("no", "")
    if state == "prefix":
        return ("prefix", "")
    return ("match", p[_envelope_head_end(p) :])


def _prefix_state(probe: str) -> tuple[str, str]:
    """Classify a RAW probe (role prefix + envelope head combined)."""
    p = probe.lstrip()
    role = _match_role_prefix(p)
    if role is None:
        return _after_role(p)
    state, rest = role
    if state == "prefix":
        return ("prefix", "")
    return _after_role(rest)


def _envelope_head_end(probe: str) -> int:
    """Index just past the envelope head within probe (match state)."""
    m = _ENVELOPE_RE.match(probe)
    return m.end() if m else len(probe)


async def unwrap_assistant_envelope(
    stream: AsyncIterator[str],
) -> AsyncIterator[str]:
    """Pass through LLM deltas, unwrapping a leading JSON envelope.

    Normal prose is passed through with ZERO added latency (the first
    delta is emitted as soon as it is proven not to be an envelope
    prefix). Only when the stream begins with an optional markdown role
    prefix (`**Assistant**: ` etc.) followed by a JSON envelope of the
    form `{"t": <anything>, "text": "` do we strip it; the inner text
    then streams immediately, sentence by sentence, and a trailing `"}`
    (possibly split across deltas) is dropped at the end. A stream that
    starts with `{` but diverges from the envelope prefix is passed
    through untouched.
    """

    pending = ""  # pre-envelope accumulation (prefix matching only)
    inside = False
    tail = ""  # rolling 2-char buffer for a closing '"}' split across deltas

    async for delta in stream:
        if not inside:
            pending += delta
            state, rest = _prefix_state(pending)
            if state == "no":
                # Diverged from the envelope prefix — pass through raw.
                yield pending
                pending = ""
                async for d in stream:
                    yield d
                return
            if state == "prefix":
                continue
            if state == "strip":
                # Role prefix consumed but no envelope follows — emit the
                # stripped text, then pass the rest of the stream through.
                if rest:
                    yield rest
                async for d in stream:
                    yield d
                return
            # state == "match": full envelope head matched; enter inside.
            inside = True
            pending = ""
            delta = rest
        else:
            delta = tail + delta
            tail = ""

        # Inside the envelope: stream inner text; watch for the closing
        # '"}' ANYWHERE in the accumulated data (normally the very end of
        # the model's reply; if the model adds text after the envelope we
        # pass that through too rather than dropping it).
        close = delta.find(_ENVELOPE_SUFFIX)
        if close >= 0:
            yield delta[:close]
            after = delta[close + len(_ENVELOPE_SUFFIX) :]
            if after:
                yield after
            async for d in stream:
                yield d
            return
        if len(delta) >= 2:
            yield delta[:-2]
            tail = delta[-2:]
        else:
            tail = delta

    # End of stream.
    if pending:
        yield pending
    if inside and tail:
        if tail.endswith(_ENVELOPE_SUFFIX):
            tail = tail[: -len(_ENVELOPE_SUFFIX)]
        if tail:
            yield tail


class AxiomGatewayLLM:
    """LLM stage → AXIOM gateway (ADR-012). SSE chunks are `{"content": s}`.

    The gateway maps `{messages, model, policy, stream, egress}`; VIHS
    context assembly (SPEC-001 D5) already matches the messages shape.
    `egress: true` asks the gateway to route through the model's bound
    egress sidecar (AXIOM L2.6).
    """

    def __init__(
        self,
        url: str,
        token: str,
        model: str | None = None,
        policy: str = "latency",
        egress: bool = True,
        timeout: float = 30.0,
        provider: str | None = None,
        verify: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.model = model
        self.policy = policy
        self.egress = egress
        self.timeout = timeout
        self.provider = provider
        self.verify = verify

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        import httpx

        url = f"{self.url}/chat/stream"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "policy": self.policy,
            "stream": True,
            "egress": self.egress,
        }
        if self.model is not None:
            body["model"] = self.model
        if self.provider is not None:
            body["provider"] = self.provider

        async with (
            httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client,
            client.stream("POST", url, json=body, headers=headers) as resp,
        ):
            if resp.status_code != 200:
                text = (await resp.aread()).decode(errors="replace")[:200]
                raise RuntimeError(f"axiom-gateway {resp.status_code}: {text}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                content = chunk.get("content")
                if isinstance(content, str) and content:
                    yield content


class VLLMLLM:
    """LLM stage → local vLLM (OpenAI-compat `/v1/chat/completions`).

    Streaming deltas arrive as `choices[0].delta.content`; the raw prompt
    string is sent as a single user message (bytes preserved by the caller
    — context assembly renders the frozen preamble).
    """

    def __init__(
        self,
        url: str,
        model: str = "default",
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.token = token
        self.timeout = timeout

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        import httpx

        url = f"{self.url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }

        async with (
            httpx.AsyncClient(timeout=self.timeout) as client,
            client.stream("POST", url, json=body, headers=headers) as resp,
        ):
            if resp.status_code != 200:
                text = (await resp.aread()).decode(errors="replace")[:200]
                raise RuntimeError(f"vllm {resp.status_code}: {text}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield content

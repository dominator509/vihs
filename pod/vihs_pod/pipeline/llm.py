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
from collections.abc import AsyncIterator
from typing import Any

# Defensive envelope unwrap (EP-010 M2): some hosted RP models wrap the
# whole reply in a JSON envelope `{"t": "assistant_output", "text": "..."}`.
# Feeding that raw into TTS is wrong twice over: piper chokes on the JSON
# syntax (curly braces/phonemization — measured: ~1.4s for the envelope
# clause vs ~226ms first chunk for plain prose) and the avatar would
# literally "speak" the JSON. This wrapper streams ONLY the inner text,
# so the unwrap happens before chunking and TTS never sees the envelope.
_ENVELOPE_PREFIX = '{"t": "assistant_output", "text": "'
_ENVELOPE_SUFFIX = '"}'


async def unwrap_assistant_envelope(
    stream: AsyncIterator[str],
) -> AsyncIterator[str]:
    """Pass through LLM deltas, unwrapping a leading assistant envelope.

    Normal prose is passed through with ZERO added latency (the first
    delta is emitted as soon as it is proven not to be an envelope
    prefix). Only when the stream begins with the envelope prefix do we
    strip it; the inner text then streams immediately, sentence by
    sentence, and a trailing `"}` (possibly split across deltas) is
    dropped at the end. A stream that starts with `{` but diverges from
    the envelope prefix is passed through untouched.
    """

    pending = ""  # pre-envelope accumulation (prefix matching only)
    inside = False
    tail = ""  # rolling 2-char buffer for a closing '"}' split across deltas

    async for delta in stream:
        if not inside:
            pending += delta
            probe = pending.lstrip()
            if probe:
                n = min(len(probe), len(_ENVELOPE_PREFIX))
                if probe[:n] != _ENVELOPE_PREFIX[:n]:
                    # Diverged from the envelope prefix — pass through raw.
                    yield pending
                    pending = ""
                    async for d in stream:
                        yield d
                    return
                if probe.startswith(_ENVELOPE_PREFIX):
                    inside = True
                    rest = probe[len(_ENVELOPE_PREFIX) :]
                    pending = ""
                    # Reuse the inside path for the rest (the closing '"}'
                    # may already be in it).
                    delta = rest
                else:
                    continue
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

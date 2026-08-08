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
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.model = model
        self.policy = policy
        self.egress = egress
        self.timeout = timeout

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

        async with (
            httpx.AsyncClient(timeout=self.timeout) as client,
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

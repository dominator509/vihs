"""Memory client (pod → memoryd). SPEC-003 memoryd rows the pod consumes:
append events, fetch transcript/memory. Async httpx — the pod agent is an
asyncio loop and every await point is a real cancellation point (real
stages swap in EP-009).
"""

from __future__ import annotations

from typing import Any, cast

import httpx


class MemoryClient:
    def __init__(self, base_url: str, pod_token: str, timeout: float = 5.0) -> None:
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {pod_token}"}
        self.timeout = timeout

    async def append_event(self, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/sessions/{id}/events — returns {status, hash, turn_id}."""
        url = f"{self.base}/v1/sessions/{session_id}/events"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=event, headers=self.headers)
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json())

    async def transcript(self, session_id: str) -> str:
        """GET /v1/sessions/{id}/transcript — rendered markdown."""
        url = f"{self.base}/v1/sessions/{session_id}/transcript"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self.headers)
            resp.raise_for_status()
            return resp.text

    async def load(self, session_id: str) -> dict[str, Any]:
        """POST /v1/sessions/{id}/load — cursor + signed memory URL."""
        url = f"{self.base}/v1/sessions/{session_id}/load"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json={}, headers=self.headers)
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json())

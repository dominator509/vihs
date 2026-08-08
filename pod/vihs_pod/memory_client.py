"""Memory client (pod → memoryd). SPEC-003 memoryd rows the pod consumes:
append events, fetch transcript/memory. Uses httpx (blocking sync client is
fine for the pod agent; real stages swap in EP-009).
"""

from __future__ import annotations

from typing import Any, cast

import httpx


class MemoryClient:
    def __init__(self, base_url: str, pod_token: str, timeout: float = 5.0) -> None:
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {pod_token}"}
        self.timeout = timeout

    def append_event(self, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/sessions/{id}/events — returns {status, hash, turn_id}."""
        url = f"{self.base}/v1/sessions/{session_id}/events"
        resp = httpx.post(url, json=event, headers=self.headers, timeout=self.timeout)
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    def transcript(self, session_id: str) -> str:
        """GET /v1/sessions/{id}/transcript — rendered markdown."""
        url = f"{self.base}/v1/sessions/{session_id}/transcript"
        resp = httpx.get(url, headers=self.headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    def load(self, session_id: str) -> dict[str, Any]:
        """POST /v1/sessions/{id}/load — cursor + signed memory URL."""
        url = f"{self.base}/v1/sessions/{session_id}/load"
        resp = httpx.post(url, json={}, headers=self.headers, timeout=self.timeout)
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

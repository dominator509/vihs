"""memory_client tests (SPEC-003 memoryd rows; M2 requires dev services).

Integration-marked: exercises the real pod→memoryd HTTP surface. Requires
memoryd + Redis + MinIO up (test-integration.sh gate).
"""

from __future__ import annotations

import os
import uuid

import pytest

from vihs_pod.memory_client import MemoryClient

MEMORYD = os.environ.get("VIHS_MEMORYD_ADDR", "127.0.0.1:8091")


@pytest.fixture
async def client() -> MemoryClient:
    return MemoryClient(f"http://{MEMORYD}", pod_token="pod-test-token")


@pytest.mark.integration
async def test_append_then_transcript(client: MemoryClient) -> None:
    sid = f"pod-test-{uuid.uuid4().hex[:8]}"
    event = {
        "v": 1,
        "session_id": sid,
        "turn_id": 1,
        "ts": "2026-07-07T18:22:31.482Z",
        "role": "user",
        "kind": "utterance",
        "text": "hello pod",
        "meta": {"interrupted": False},
    }
    resp = await client.append_event(sid, event)
    assert resp["status"] in ("committed", "duplicate")
    assert resp["hash"], "committed event carries a chain hash"
    assert resp["turn_id"] == 1


@pytest.mark.integration
async def test_load_unknown_session_raises(client: MemoryClient) -> None:
    from httpx import HTTPStatusError

    with pytest.raises(HTTPStatusError):
        await client.load(f"nope-{uuid.uuid4().hex[:8]}")

"""memory_client tests (SPEC-003 memoryd rows; M2 requires dev services).

Integration-marked: exercises the real pod→memoryd HTTP surface. Requires
memoryd + Redis + MinIO up (test-integration.sh gate).

EP-006 M3: memoryd now verifies every bearer with the shared token store, so
the fixture mints a REAL user token through the orchestrator's admin mint
route (spawning the orchestrator if it is not already running). The token is
a bearer for the HTTP surface under test — pod-token *semantics* (session
binding, allowed verbs) are covered by the Rust authz suite
(crates/memoryd/tests/authz.rs), not here.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

from vihs_pod.memory_client import MemoryClient

MEMORYD = os.environ.get("VIHS_MEMORYD_ADDR", "127.0.0.1:8091")
ORCH = os.environ.get("VIHS_ORCH_ADDR", "127.0.0.1:8080")
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _wait_http(url: str, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _ensure_orchestrator() -> None:
    if _wait_http(f"http://{ORCH}/healthz", timeout=3.0):
        return
    env = {**os.environ, **_load_env()}
    subprocess.Popen(
        [str(ROOT / "target" / "debug" / "orchestrator")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_http(f"http://{ORCH}/healthz"):
        raise RuntimeError("orchestrator did not become healthy for mint")


def _mint_user_token() -> str:
    _ensure_orchestrator()
    env = _load_env()
    admin = env.get("VIHS_ADMIN_TOKEN", "")
    if not admin:
        raise RuntimeError("VIHS_ADMIN_TOKEN missing from .env (bootstrap admin token)")
    data = json.dumps({"owner_id": "pod-test-owner", "scope": "user"}).encode()
    req = urllib.request.Request(
        f"http://{ORCH}/admin/tokens",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {admin}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"mint user token failed: {e.code} {e.read()!r}") from e
    tok = body.get("token", "")
    if not tok:
        raise RuntimeError(f"mint response missing token: {body}")
    return tok


@pytest.fixture
async def client() -> MemoryClient:
    token = _mint_user_token()
    # The second arg is the bearer for every memoryd call; a real minted
    # user token satisfies the strict authorizer (EP-006 M3).
    return MemoryClient(f"http://{MEMORYD}", pod_token=token)


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
    transcript = await client.transcript(sid)
    assert "hello pod" in transcript


@pytest.mark.integration
async def test_load_unknown_session_raises(client: MemoryClient) -> None:
    from httpx import HTTPStatusError

    with pytest.raises(HTTPStatusError):
        await client.load(f"nope-{uuid.uuid4().hex[:8]}")

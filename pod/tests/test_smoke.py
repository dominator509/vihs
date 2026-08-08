"""Smoke tests for the pod agent (EP-005 M4).

The EP-001 stub (prints "pod ready") is gone: the agent now registers with
the orchestrator and runs the assign loop, which needs live services — the
e2e harness covers that path. These unit tests pin the pure contract: URL
construction and the SPEC-003 health shape.
"""

from __future__ import annotations

from vihs_pod.agent import PodAgent, _assign_ws_url, _http_base


def test_http_base_rewrites_wildcard_bind() -> None:
    assert _http_base("0.0.0.0:8080") == "http://127.0.0.1:8080"
    assert _http_base("127.0.0.1:8093") == "http://127.0.0.1:8093"
    assert _http_base("localhost:9000") == "http://localhost:9000"


def test_assign_ws_url() -> None:
    url = _assign_ws_url("0.0.0.0:8080", "pod-1")
    assert url == "ws://127.0.0.1:8080/internal/pods/pod-1/assign"


def test_health_shape_matches_spec003() -> None:
    agent = PodAgent(
        pod_id="p1",
        pod_addr="127.0.0.1:8093",
        orch_addr="127.0.0.1:8080",
        token="t",
        cap=2,
    )
    h = agent.health()
    assert set(h["stages"]) == {"vad", "stt", "llm", "tts", "lipsync", "mux"}
    assert all(v == "ready" for v in h["stages"].values())
    assert h["fill"] == 0
    assert h["cap"] == 2

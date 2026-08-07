"""Smoke test for the pod agent entrypoint."""

from vihs_pod import agent


def test_mock_gpu_flag() -> None:
    assert agent.main(["--mock-gpu"]) == 0


def test_real_default() -> None:
    assert agent.main([]) == 0

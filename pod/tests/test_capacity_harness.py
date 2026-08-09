"""Capacity harness unit tests (EP-007 M4).

Covers:
1. `Metrics.aggregate` — merging per-conversation samples into a pod-level
   histogram preserves percentiles across all sessions.
2. `build_stages` env-gated mock latency injection — `VIHS_MOCK_*_MS` is
   honored so the CI capacity ramp can simulate realistic stage budgets
   (SPEC-008 P2/P3 CI mode), and defaults stay 0.
3. `derive_report` — the sessions_per_gpu derivation + binding constraint
   logic (ARCHITECTURE §13): VRAM bound, latency breach, no-breach cap.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from vihs_pod.conversation import build_stages
from vihs_pod.metrics import Metrics

# tests/load is not a package on the pod test path; make it importable so
# the derivation logic is unit-tested here (same pattern as chaos scripts).
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from load.capacity import derive_report  # noqa: E402


def test_aggregate_merges_samples_across_instances() -> None:
    a = Metrics()
    b = Metrics()
    for i in range(10):
        a.record("llm_ttft", 100.0 + i)
        b.record("llm_ttft", 200.0 + i)
    for i in range(5):
        a.record("tts_ttfa", 50.0 + i)

    agg = Metrics.aggregate([a, b])
    report = agg.report()

    assert report["llm_ttft"]["count"] == 20
    # Combined 10 samples of 100..109 and 10 of 200..209 → p95 is the 19th
    # of 20 sorted (0.95*20 = 19) → 209.
    assert report["llm_ttft"]["p95"] == 209.0
    # Only a recorded tts_ttfa.
    assert report["tts_ttfa"]["count"] == 5
    assert report["lipsync_ff"]["count"] == 0


def test_aggregate_empty_instances() -> None:
    agg = Metrics.aggregate([])
    report = agg.report()
    for stage in ("llm_ttft", "tts_ttfa", "lipsync_ff", "e2e_first_frame", "e2e_total"):
        assert report[stage]["count"] == 0


def test_build_stages_env_latency_injection() -> None:
    os.environ["VIHS_MOCK_LLM_TTFT_MS"] = "150"
    os.environ["VIHS_MOCK_TTS_TTFA_MS"] = "100"
    os.environ["VIHS_MOCK_LIPSYNC_FF_MS"] = "250"
    try:
        st, _monitor = build_stages(answers=["hi"], real=False)
        assert st.llm.ttft == 0.150, st.llm.ttft
        assert st.tts.ttfa == 0.100, st.tts.ttfa
        assert st.lipsync.ff == 0.250, st.lipsync.ff
    finally:
        for k in ("VIHS_MOCK_LLM_TTFT_MS", "VIHS_MOCK_TTS_TTFA_MS", "VIHS_MOCK_LIPSYNC_FF_MS"):
            os.environ.pop(k, None)


def test_build_stages_defaults_zero() -> None:
    for k in ("VIHS_MOCK_LLM_TTFT_MS", "VIHS_MOCK_TTS_TTFA_MS", "VIHS_MOCK_LIPSYNC_FF_MS"):
        os.environ.pop(k, None)
    st, _monitor = build_stages(answers=["hi"], real=False)
    assert st.llm.ttft == 0.0
    assert st.tts.ttfa == 0.0
    assert st.lipsync.ff == 0.0


def _fake_rows(oks: list[tuple[int, bool, str | None]]) -> list[dict]:
    return [{"n": n, "p95": {}, "ok": ok, "breach_stage": breach} for n, ok, breach in oks]


def test_derive_report_vram_binds_first(monkeypatch) -> None:
    # No latency breach up to cap=4, but VRAM allows only 2.
    monkeypatch.setenv("VIHS_VRAM_AVAILABLE_GB", "6")
    monkeypatch.setenv("VIHS_VRAM_PER_SESSION_GB", "3")
    report = derive_report(
        _fake_rows([(1, True, None), (2, True, None), (3, True, None), (4, True, None)])
    )
    assert report["sessions_per_gpu"] == 2
    assert report["constraint"] == "vram"


def test_derive_report_latency_breach_stops_at_first() -> None:
    report = derive_report(_fake_rows([(1, True, None), (2, False, "lipsync_ff"), (3, True, None)]))
    assert report["sessions_per_gpu"] == 1
    assert report["constraint"] == "lipsync_ff"


def test_derive_report_no_breach_cap_is_bound() -> None:
    report = derive_report(_fake_rows([(1, True, None), (2, True, None)]))
    assert report["sessions_per_gpu"] == 2
    assert "cap" in report["constraint"]

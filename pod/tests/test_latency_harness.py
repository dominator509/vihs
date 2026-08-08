"""Latency harness (EP-005 M6, ARCHITECTURE §6 budget table).

Tunes the mock stages to their budget MIDPOINTS and runs one scripted turn
through the real task graph, then asserts:

1. Every critical-path stage histogram is populated (LLM TTFT, TTS TTFA,
   lip-sync first frame, e2e first-frame, e2e total).
2. Pipeline-overhead assertion: e2e first-frame ≤ sum of stage midpoints
   + 150 ms slack. The mocks are pipelined (clause granularity), so the
   critical path is LLM TTFT → TTS TTFA → lip-sync first frame; the 150 ms
   slack absorbs scheduler/queue handoff overhead.
"""

from __future__ import annotations

from vihs_pod.metrics import Metrics
from vihs_pod.mocks.stages import MockLipSync, MockLLM, MockMux, MockTTS
from vihs_pod.pipeline.abort_bus import AbortBus
from vihs_pod.pipeline.flow import run_response

# ARCHITECTURE §6 budget midpoints (seconds).
LLM_TTFT_MID = 0.275  # 150–400 ms
TTS_TTFA_MID = 0.200  # 100–300 ms
LIPSYNC_FF_MID = 0.250  # 100–400 ms
SLACK_MS = 150.0


class LatencyStages:
    def __init__(self) -> None:
        self.llm = MockLLM(
            response="Hello there. This is a longer answer. How are you?",
            delay=0.0005,
            ttft=LLM_TTFT_MID,
        )
        self.tts = MockTTS(ms_per_char=10, ttfa=TTS_TTFA_MID)
        self.lipsync = MockLipSync(ff=LIPSYNC_FF_MID)
        self.mux = MockMux()


async def test_latency_harness_populates_all_histograms() -> None:
    bus = AbortBus()
    st = LatencyStages()
    metrics = Metrics()
    ledger: dict[int, str] = {}

    committed = await run_response(
        bus.fresh(), bus, st, "user prompt", turn_id=1, ledger=ledger, metrics=metrics
    )

    assert not committed.interrupted
    assert metrics.populated(), f"all histograms must be populated:\n{metrics.fmt_report()}"

    report = metrics.report()
    # Budget midpoints are ~250 ms; sanity bound keeps CI honest (a mock
    # that stops sleeping would collapse to ~0 and hide regressions).
    for stage in ("llm_ttft", "tts_ttfa", "lipsync_ff"):
        assert report[stage]["p50"] > 100.0, f"{stage} below budget floor:\n{metrics.fmt_report()}"


async def test_pipeline_overhead_within_budget_slack() -> None:
    """e2e first-frame ≤ sum of stage midpoints + 150 ms slack."""
    bus = AbortBus()
    st = LatencyStages()
    metrics = Metrics()
    ledger: dict[int, str] = {}

    await run_response(
        bus.fresh(), bus, st, "user prompt", turn_id=1, ledger=ledger, metrics=metrics
    )

    report = metrics.report()
    first_frame = report["e2e_first_frame"]["p50"]
    budget_ms = (LLM_TTFT_MID + TTS_TTFA_MID + LIPSYNC_FF_MID) * 1000.0 + SLACK_MS
    msg = (
        f"e2e first-frame {first_frame:.1f} ms exceeds budget {budget_ms:.1f} ms:\n"
        f"{metrics.fmt_report()}"
    )
    assert first_frame <= budget_ms, msg

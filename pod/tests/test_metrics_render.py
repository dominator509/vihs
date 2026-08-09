"""EP-008 M2: pod metrics render — Prometheus text exposition.

Asserts the registry series (OBSERVABILITY.md) appear with pod_id/model_ver
labels after a scripted turn, and that counters/gauges render.
"""

from __future__ import annotations

import asyncio

from vihs_pod.metrics import Metrics, PodMetrics, render_text
from vihs_pod.mocks.stages import MockLipSync, MockLLM, MockMux, MockTTS
from vihs_pod.pipeline.abort_bus import AbortBus
from vihs_pod.pipeline.flow import run_response

REQUIRED_SERIES = [
    "vihs_stage_first_chunk_ms",
    "vihs_e2e_first_audio_ms",
    "vihs_e2e_total_ms",
    "vihs_bargein_total",
    "vihs_abort_flush_ms",
    "vihs_endpoint_premature_total",
    "vihs_append_buffer_depth",
    "vihs_prefix_cache_hit_ratio",
    "vihs_epoch_boundary_total",
    "vihs_gpu_util",
]


class Stages:
    def __init__(self, llm: str, ms_per_char: int = 10) -> None:
        self.llm = MockLLM(response=llm, delay=0.0005)
        self.tts = MockTTS(ms_per_char=ms_per_char)
        self.lipsync = MockLipSync()
        self.mux = MockMux()


async def test_metrics_render_includes_all_registry_series() -> None:
    """One scripted turn + a barge-in → every required series renders."""
    bus = AbortBus()
    st = Stages("Hello there. This is a longer answer. How are you?")
    metrics = Metrics()
    ledger: dict[int, str] = {}

    task = asyncio.create_task(
        run_response(
            bus.fresh(),
            bus,
            st,
            "user prompt",
            turn_id=1,
            ledger=ledger,
            metrics=metrics,
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    del task

    pod_metrics = PodMetrics()
    pod_metrics.record_bargein(12.5)
    pod_metrics.append_buffer_depth = 2
    pod_metrics.prefix_cache_hit_ratio = 0.95

    body = render_text(metrics, pod_metrics, pod_id="pod-1", model_ver="mock")
    for name in REQUIRED_SERIES:
        assert name in body, f"missing series {name} in:\n{body}"

    # Labels are pod_id + model_ver on every family.
    assert 'pod_id="pod-1"' in body
    assert 'model_ver="mock"' in body

    # The barge-in sample landed in the abort-flush histogram.
    assert 'vihs_bargein_total{pod_id="pod-1",model_ver="mock"} 1' in body
    assert "vihs_abort_flush_ms_count" in body


async def test_metrics_render_empty_histograms_still_present() -> None:
    """A pod with no traffic still advertises the full registry (0 samples)."""
    body = render_text(Metrics(), PodMetrics(), pod_id="pod-2", model_ver="mock")
    for name in REQUIRED_SERIES:
        assert name in body, f"missing series {name} in:\n{body}"
    # Empty histogram still has a +Inf bucket = 0.
    empty_bucket = (
        'vihs_stage_first_chunk_ms_bucket{pod_id="pod-2",model_ver="mock",'
        'stage="llm_ttft",le="+Inf"} 0'
    )
    assert empty_bucket in body

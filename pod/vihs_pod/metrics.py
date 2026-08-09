"""Latency instrumentation (EP-005 M6, ARCHITECTURE §6 budget table).

Records first-chunk latency samples per pipeline stage (LLM TTFT, TTS TTFA,
lip-sync first frame, e2e first-frame, e2e total) and renders a histogram
report. Pure stdlib, no deps; safe to construct per-turn and pass through
the task graph. Thread-safety: the pod is asyncio-single-threaded, so a
plain list append from pipeline tasks is safe.

EP-008 M2 adds Prometheus text exposition: stage histograms + counters +
gauges rendered exactly per OBSERVABILITY.md (labels pod_id, model_ver).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")

# Fixed histogram buckets (ms) for stage first-chunk and e2e latency —
# aligned with ARCHITECTURE §6 budget lines (e2e p95 target 2000 ms,
# stage budgets < 400 ms for the slow stages).
STAGE_BUCKETS_MS = [50.0, 100.0, 200.0, 400.0, 800.0, 1600.0, 3200.0, 6400.0]
E2E_BUCKETS_MS = [250.0, 500.0, 1000.0, 1500.0, 2000.0, 3000.0, 4000.0, 6000.0]
ABORT_FLUSH_BUCKETS_MS = [10.0, 25.0, 50.0, 75.0, 100.0, 150.0, 200.0, 300.0]

# Registry stage labels (OBSERVABILITY.md) — subset the pod currently
# records (mock stages cover LLM/TTS/lipsync; vad/stt/clause/network
# histograms populate when real stages land).
STAGE_LABELS = ("llm_ttft", "tts_ttfa", "lipsync_ttff", "e2e_first_audio", "e2e_total")


@dataclass
class Metrics:
    """Collects stage latency samples and produces a percentile report."""

    llm_ttft_ms: list[float] = field(default_factory=list)
    tts_ttfa_ms: list[float] = field(default_factory=list)
    lipsync_ff_ms: list[float] = field(default_factory=list)
    e2e_first_frame_ms: list[float] = field(default_factory=list)
    e2e_total_ms: list[float] = field(default_factory=list)
    _e2e_ff_recorded: bool = field(default=False, init=False, repr=False)

    def record(self, stage: str, ms: float) -> None:
        """Record a single first-chunk latency sample for a stage."""
        bucket = getattr(self, f"{stage}_ms", None)
        if bucket is None:
            raise KeyError(f"unknown stage: {stage}")
        bucket.append(ms)

    def report(self) -> dict[str, dict[str, float]]:
        """Percentile report per stage: count/min/p50/p95/max/mean."""
        out: dict[str, dict[str, float]] = {}
        for stage in (
            "llm_ttft",
            "tts_ttfa",
            "lipsync_ff",
            "e2e_first_frame",
            "e2e_total",
        ):
            samples = sorted(getattr(self, f"{stage}_ms"))
            if not samples:
                out[stage] = {"count": 0.0}
                continue
            n = len(samples)

            def _pct(q: float, _samples: list[float] = samples, _n: int = n) -> float:
                return _samples[min(_n - 1, int(q * _n))]

            out[stage] = {
                "count": float(n),
                "min": samples[0],
                "p50": _pct(0.50),
                "p95": _pct(0.95),
                "max": samples[-1],
                "mean": sum(samples) / n,
            }
        return out

    def populated(self) -> bool:
        """True when every critical-path histogram has at least one sample."""
        return all(
            getattr(self, f"{stage}_ms")
            for stage in ("llm_ttft", "tts_ttfa", "lipsync_ff", "e2e_first_frame", "e2e_total")
        )

    @classmethod
    def aggregate(cls, instances: list[Metrics]) -> Metrics:
        """Merge per-conversation samples into one pod-level histogram set.

        The capacity harness (EP-007 M4) ramps concurrent sessions on one
        pod and needs a single per-stage percentile view across ALL of them
        (SPEC-007 O1 labels the histogram pod_id — the aggregate is the
        pod's histogram).
        """
        out = cls()
        for stage in (
            "llm_ttft",
            "tts_ttfa",
            "lipsync_ff",
            "e2e_first_frame",
            "e2e_total",
        ):
            bucket = getattr(out, f"{stage}_ms")
            for inst in instances:
                bucket.extend(getattr(inst, f"{stage}_ms"))
        return out

    def fmt_report(self) -> str:
        rows = ["stage                 count   min    p50    p95    max"]
        for stage, s in self.report().items():
            if s["count"] == 0:
                rows.append(f"{stage:<20}  {0:>5}   -      -      -      -")
                continue
            rows.append(
                f"{stage:<20}  {int(s['count']):>5}  {s['min']:>5.1f}  "
                f"{s['p50']:>5.1f}  {s['p95']:>5.1f}  {s['max']:>5.1f}"
            )
        return "\n".join(rows)


def timed(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[tuple[T, float]]]:
    """Decorator: run fn, return (result, elapsed_ms)."""

    async def wrapper(*args: Any, **kwargs: Any) -> tuple[T, float]:
        start = time.perf_counter()
        result = await fn(*args, **kwargs)
        return result, (time.perf_counter() - start) * 1000.0

    return wrapper


# ---------------------------------------------------------------------------
# EP-008 M2 — Prometheus text exposition (pod /metrics)
# ---------------------------------------------------------------------------


def _fmt_float(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _histogram_lines(
    name: str,
    help_: str,
    samples: list[float],
    buckets: list[float],
    labels: str,
    emit_help: bool = True,
) -> list[str]:
    """Render one Prometheus histogram family from raw samples.

    `labels` is the base label set WITHOUT braces or the le label, e.g.
    `pod_id="p",model_ver="m",stage="s"` — le joins the same brace group
    (Prometheus requires a single label set per series). `emit_help=False`
    lets a caller group multiple label sets under ONE HELP/TYPE (the text
    format allows only one HELP/TYPE per metric name).
    """
    lines: list[str] = []
    if emit_help:
        lines = [f"# HELP {name} {help_}", f"# TYPE {name} histogram"]
    n = len(samples)
    label_prefix = f"{{{labels}," if labels else "{"
    if n == 0:
        lines.append(f'{name}_bucket{label_prefix}le="+Inf"}} 0')
        lines.append(f"{name}_sum{{{labels}}} 0")
        lines.append(f"{name}_count{{{labels}}} 0")
        return lines
    for b in buckets:
        le = sum(1 for s in samples if s <= b)
        lines.append(f'{name}_bucket{label_prefix}le="{_fmt_float(b)}"}} {le}')
    lines.append(f'{name}_bucket{label_prefix}le="+Inf"}} {n}')
    lines.append(f"{name}_sum{{{labels}}} {_fmt_float(sum(samples))}")
    lines.append(f"{name}_count{{{labels}}} {n}")
    return lines


@dataclass
class PodMetrics:
    """Pod-level counters/gauges (OBSERVABILITY.md registry, EP-008 M2).

    Histogram samples live in per-conversation `Metrics`; this holds the
    turn-taking counters, memory gauges, and cache-ratio/gpu gauges the
    registry requires. Rendered together via `render_text`.
    """

    bargein_total: int = 0
    endpoint_premature_total: int = 0
    epoch_boundary_total: int = 0
    abort_flush_ms: list[float] = field(default_factory=list)
    append_buffer_depth: int = 0
    prefix_cache_hit_ratio: float = 0.0
    gpu_util: float = 0.0

    def record_bargein(self, flush_ms: float) -> None:
        self.bargein_total += 1
        self.abort_flush_ms.append(flush_ms)

    def record_endpoint_premature(self) -> None:
        self.endpoint_premature_total += 1

    def record_epoch_boundary(self) -> None:
        self.epoch_boundary_total += 1


def render_text(
    metrics: Metrics,
    pod_metrics: PodMetrics,
    pod_id: str,
    model_ver: str,
) -> str:
    """Render the pod's full metric set as Prometheus text exposition.

    Labels: pod_id + model_ver on every series (SPEC-007 O1). The stage
    histograms map the pod's recorded samples (llm_ttft/tts_ttfa/lipsync_ff/
    e2e_first_frame/e2e_total) onto the registry names; e2e_first_frame is
    the pod's first-avatar-output sample, exposed as `vihs_e2e_first_audio_ms`
    (the product number per OBSERVABILITY.md).
    """
    out: list[str] = []
    labels = f'pod_id="{pod_id}",model_ver="{model_ver}"'

    # Stage first-chunk histograms: ONE family `vihs_stage_first_chunk_ms`
    # labeled by stage (Prometheus text format allows a single HELP/TYPE per
    # metric name). Each recorded stage contributes its bucket series.
    stage_samples = {
        "llm_ttft": metrics.llm_ttft_ms,
        "tts_ttfa": metrics.tts_ttfa_ms,
        "lipsync_ttff": metrics.lipsync_ff_ms,
    }
    out.append("# HELP vihs_stage_first_chunk_ms First-chunk latency per pipeline stage (ms).")
    out.append("# TYPE vihs_stage_first_chunk_ms histogram")
    for stage, samples in stage_samples.items():
        out.extend(
            _histogram_lines(
                "vihs_stage_first_chunk_ms",
                "First-chunk latency per pipeline stage (ms).",
                samples,
                STAGE_BUCKETS_MS,
                f'pod_id="{pod_id}",model_ver="{model_ver}",stage="{stage}"',
                emit_help=False,
            )
        )

    # e2e first audio + e2e total (both registry names).
    out.extend(
        _histogram_lines(
            "vihs_e2e_first_audio_ms",
            "Stop-speaking to first avatar audio (ms).",
            metrics.e2e_first_frame_ms,
            E2E_BUCKETS_MS,
            labels,
        )
    )
    out.extend(
        _histogram_lines(
            "vihs_e2e_total_ms",
            "Full turn response duration (ms).",
            metrics.e2e_total_ms,
            E2E_BUCKETS_MS,
            labels,
        )
    )

    # Turn-taking counters.
    out.append("# HELP vihs_bargein_total Barge-in events (user interrupted playback).")
    out.append("# TYPE vihs_bargein_total counter")
    out.append(f"vihs_bargein_total{{{labels}}} {pod_metrics.bargein_total}")
    out.append("# HELP vihs_endpoint_premature_total User spoke <300 ms after endpoint.")
    out.append("# TYPE vihs_endpoint_premature_total counter")
    out.append(f"vihs_endpoint_premature_total{{{labels}}} {pod_metrics.endpoint_premature_total}")
    out.extend(
        _histogram_lines(
            "vihs_abort_flush_ms",
            "Barge-in abort flush duration (ms; budget ≤100).",
            pod_metrics.abort_flush_ms,
            ABORT_FLUSH_BUCKETS_MS,
            labels,
        )
    )

    # Memory gauges.
    out.append("# HELP vihs_append_buffer_depth Pending appends in the pod buffer.")
    out.append("# TYPE vihs_append_buffer_depth gauge")
    out.append(f"vihs_append_buffer_depth{{{labels}}} {pod_metrics.append_buffer_depth}")
    out.append("# HELP vihs_prefix_cache_hit_ratio LLM prefix-cache hit ratio (INV-4).")
    out.append("# TYPE vihs_prefix_cache_hit_ratio gauge")
    ratio = _fmt_float(pod_metrics.prefix_cache_hit_ratio)
    out.append(f"vihs_prefix_cache_hit_ratio{{{labels}}} {ratio}")
    out.append("# HELP vihs_epoch_boundary_total Memory epoch boundaries observed by the pod.")
    out.append("# TYPE vihs_epoch_boundary_total counter")
    out.append(f"vihs_epoch_boundary_total{{{labels}}} {pod_metrics.epoch_boundary_total}")

    # Fleet gauges (pod-local view).
    out.append("# HELP vihs_gpu_util GPU utilization (0 in mock mode).")
    out.append("# TYPE vihs_gpu_util gauge")
    out.append(f"vihs_gpu_util{{{labels}}} {_fmt_float(pod_metrics.gpu_util)}")

    return "\n".join(out) + "\n"

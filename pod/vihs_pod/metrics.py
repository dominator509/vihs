"""Latency instrumentation (EP-005 M6, ARCHITECTURE §6 budget table).

Records first-chunk latency samples per pipeline stage (LLM TTFT, TTS TTFA,
lip-sync first frame, e2e first-frame, e2e total) and renders a histogram
report. Pure stdlib, no deps; safe to construct per-turn and pass through
the task graph. Thread-safety: the pod is asyncio-single-threaded, so a
plain list append from pipeline tasks is safe.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


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

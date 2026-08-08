"""AppendBuffer unit tests (EP-007 M2; ARCHITECTURE §9 / SPEC-006 R2).

The buffer guarantees:
- enqueue never blocks/awaits network (synchronous, bounded).
- a background flusher drains to memoryd with retry/backoff.
- on full, the buffer degrades (sticky until drained).
- events flush in FIFO order.
"""

from __future__ import annotations

import asyncio

import pytest

from vihs_pod.append_buffer import MAX_BUFFER, AppendBuffer


class FlakyMemory:
    """Test double: fails append N times, then succeeds; records order."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.appended: list[dict] = []

    async def append_event(self, session_id: str, event: dict) -> dict:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("memoryd down (test)")
        self.appended.append(event)
        return {"status": "committed", "hash": "h", "turn_id": 1}


def ev(n: int) -> dict:
    return {"turn_id": n, "text": f"event-{n}"}


@pytest.mark.asyncio
async def test_enqueue_is_synchronous_and_non_blocking() -> None:
    """enqueue must not await anything — the media path never blocks."""
    mem = FlakyMemory()
    buf = AppendBuffer(mem, "s1")
    # A plain synchronous call with a running loop that never yields: if
    # enqueue awaited, this would deadlock/timeout.
    for i in range(10):
        assert buf.enqueue(ev(i)) is True
    assert buf.depth == 10


@pytest.mark.asyncio
async def test_flusher_drains_in_order() -> None:
    mem = FlakyMemory()
    buf = AppendBuffer(mem, "s1")
    buf.start()
    for i in range(5):
        buf.enqueue(ev(i))
    # Give the flusher a moment.
    for _ in range(100):
        if buf.depth == 0:
            break
        await asyncio.sleep(0.01)
    assert buf.depth == 0
    assert [e["turn_id"] for e in mem.appended] == [0, 1, 2, 3, 4]
    await buf.stop()


@pytest.mark.asyncio
async def test_retry_with_backoff_after_transient_failure() -> None:
    mem = FlakyMemory(fail_times=2)
    buf = AppendBuffer(mem, "s1")
    buf.start()
    buf.enqueue(ev(7))
    for _ in range(200):
        if buf.depth == 0 and mem.appended:
            break
        await asyncio.sleep(0.01)
    assert mem.appended == [ev(7)], "event eventually flushed after retries"
    assert buf.degraded is False
    await buf.stop()


@pytest.mark.asyncio
async def test_full_buffer_degrades_and_drops() -> None:
    mem = FlakyMemory()
    buf = AppendBuffer(mem, "s1", max_items=3)
    assert buf.enqueue(ev(1))
    assert buf.enqueue(ev(2))
    assert buf.enqueue(ev(3))
    # Fourth enqueue on a full queue must NOT block and must degrade.
    assert buf.enqueue(ev(4)) is False
    assert buf.degraded is True
    assert buf.depth == 3


@pytest.mark.asyncio
async def test_degrades_clears_after_drain() -> None:
    mem = FlakyMemory()
    buf = AppendBuffer(mem, "s1", max_items=3)
    buf.start()
    buf.enqueue(ev(1))
    buf.enqueue(ev(2))
    buf.enqueue(ev(3))
    buf.enqueue(ev(4))  # full → degraded
    assert buf.degraded is True
    for _ in range(200):
        if buf.depth == 0:
            break
        await asyncio.sleep(0.01)
    assert buf.depth == 0
    assert buf.degraded is False, "degraded clears once drained"
    await buf.stop()


@pytest.mark.asyncio
async def test_flush_drains_queued_events() -> None:
    mem = FlakyMemory()
    buf = AppendBuffer(mem, "s1")
    for i in range(4):
        buf.enqueue(ev(i))
    n = await buf.flush()
    assert n == 4
    assert buf.depth == 0
    assert [e["turn_id"] for e in mem.appended] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_max_buffer_constant_is_64() -> None:
    # ARCHITECTURE §9: bounded local queue, RAM, max 64.
    assert MAX_BUFFER == 64

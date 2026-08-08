"""Append buffer (pod side) — ARCHITECTURE §9 / SPEC-006 R2.

The pod must never block its media path on memoryd. Every committed-turn
event is enqueued here (bounded, RAM, max 64) and a background flusher task
drains it to memoryd with retry/backoff. If the buffer fills, the pod
health-degrades (advertised via /health) so the orchestrator drains it —
the session survives on another pod (SPEC-006 R2: "bounded QUEUE of 64,
health-degrade on full").

Contract:
- `enqueue` is synchronous and NEVER awaits network — the conversation's
  `_append_event` calls it and returns immediately.
- `depth` is the number of events waiting (or being retried).
- `degraded` is True once the queue was ever full (sticky until drained).
- `drain` is the flusher loop: one task per pod, spawned at assignment.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

log = logging.getLogger("vihs_pod.append_buffer")

# ARCHITECTURE §9: bounded local queue, RAM, max 64.
MAX_BUFFER = 64
# SPEC-006 R1 backoff: jittered exponential, base 250 ms, cap 5 s.
BACKOFF_BASE_MS = 250.0
BACKOFF_CAP_S = 5.0


class AppendBuffer:
    def __init__(
        self,
        memory_client: Any,
        session_id: str,
        max_items: int = MAX_BUFFER,
    ) -> None:
        self._memory = memory_client
        self._session_id = session_id
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_items)
        self._max = max_items
        self._degraded = False
        self._flusher: asyncio.Task[Any] | None = None
        self._flush_lock = asyncio.Lock()
        # An event the flusher has picked up but is still retrying — it is
        # OUTSTANDING, so it must count toward depth (otherwise depth would
        # drop to 0 while memoryd is down and the append is stuck retrying).
        self._in_flight: dict[str, Any] | None = None

    # --- producers (conversation) ---

    def enqueue(self, event: dict[str, Any]) -> bool:
        """Queue an event. NEVER blocks. Returns True on success; False when
        the buffer is full (event dropped, pod must health-degrade)."""
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self._degraded = True
            log.error(
                "append buffer FULL (%d) — dropping event; pod health-degrades (ARCHITECTURE §9)",
                self._max,
            )
            return False

    @property
    def depth(self) -> int:
        """Outstanding events: waiting in the queue OR in-flight (being
        retried). This is what the chaos suite observes — while memoryd is
        frozen the flusher holds one event in retry, so depth must not drop
        to 0 until the append actually lands."""
        return self._queue.qsize() + (1 if self._in_flight is not None else 0)

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def in_flight(self) -> int:
        return 1 if self._in_flight is not None else 0

    @property
    def flusher_alive(self) -> bool:
        return self._flusher is not None and not self._flusher.done()

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def max(self) -> int:
        return self._max

    # --- lifecycle (assignment start/stop) ---

    def start(self) -> None:
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_loop(), name="append-flush")

    async def stop(self) -> None:
        """Cancel the flusher after a best-effort drain of what is queued."""
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
            self._flusher = None

    async def flush(self) -> int:
        """Drain everything currently queued (used at stop and by tests).
        Returns the number of events successfully flushed."""
        flushed = 0
        while not self._queue.empty():
            event = self._queue.get_nowait()
            try:
                await self._memory.append_event(self._session_id, event)
                flushed += 1
            except Exception:  # noqa: BLE001 — retry loop handles transient
                # Re-queue at the front; the flusher task will retry.
                self._queue.put_nowait(event)
                break
        return flushed

    # --- flusher task ---

    async def _flush_loop(self) -> None:
        attempts = 0
        while True:
            event = await self._queue.get()
            self._in_flight = event
            log.info(
                "append flusher picked up event turn=%s role=%s (queued left=%d)",
                event.get("turn_id"),
                event.get("role"),
                self._queue.qsize(),
            )
            while True:
                try:
                    await self._memory.append_event(self._session_id, event)
                    attempts = 0
                    self._in_flight = None
                    if self._queue.empty():
                        self._degraded = False  # drained — clear degrade
                    break
                except asyncio.CancelledError:
                    self._in_flight = None
                    raise
                except Exception as e:  # noqa: BLE001
                    attempts += 1
                    delay = min(
                        BACKOFF_BASE_MS / 1000.0 * (2 ** min(attempts - 1, 6)),
                        BACKOFF_CAP_S,
                    )
                    # SPEC-006 logging: retries log at debug, not info/warn
                    # (no log storms while memoryd is down). Health surfaces
                    # depth/degraded for operators.
                    log.debug(
                        "append retry %d for session=%s in %.1fs: %s",
                        attempts,
                        self._session_id,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)

"""AbortBus — the one concurrency primitive that keeps the pipeline honest
(SPEC-001 D3).

A generation counter guards the whole response. Stale stages see `gen`
mismatch and drop output; on abort every task is cancelled, queues flushed,
the renderer neutraled, and the playback ledger returned so INV-1 committed
text can be reconstructed exactly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlayedSpan:
    """A span of rendered text the mux actually pushed on-wire (INV-1)."""

    clause_id: int
    char_start: int
    char_end: int


class AbortBus:
    def __init__(self) -> None:
        self.gen = 0
        self._tasks: set[asyncio.Task[Any]] = set()

    def guard(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def abort(
        self,
        mux: Callable[[], Awaitable[list[PlayedSpan]]],
        renderer: Callable[[], Awaitable[None]],
    ) -> list[PlayedSpan]:
        """Bump generation, cancel all guarded tasks, resolve the ledger.

        Contract: returns within the internal completion budget (≤100 ms with
        cancellation-safe mock stages).
        """
        self.gen += 1  # stale stages see gen mismatch and drop output
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        played = await mux()
        await renderer()
        return played

    def fresh(self) -> int:
        """The current generation — capture at response start, compare in loops."""
        return self.gen

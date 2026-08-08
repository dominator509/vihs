"""Clause-boundary chunker (SPEC-001 D2) — LLM → TTS handoff.

Pure incremental string chunker. Emits completable clauses the moment they
bound; TTS never waits for the full response.
"""

from __future__ import annotations

ABBREV = {"mr.", "mrs.", "dr.", "e.g.", "i.e.", "etc.", "vs."}


class ClauseChunker:
    """SPEC-001 D2 reference chunker."""

    def __init__(self, min_chars: int = 40, max_chars: int = 240) -> None:
        self.buf = ""
        self.min = min_chars
        self.max = max_chars

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            cut = self._boundary()
            if cut is None:
                break
            out.append(self.buf[:cut])
            # Keep the remainder verbatim: the D2 invariant is that emitted
            # clauses + residue concatenate EXACTLY to the input. lstrip()
            # would eat the space after a boundary and break that invariant.
            self.buf = self.buf[cut:]
        return out

    def _boundary(self) -> int | None:
        if len(self.buf) >= self.max:
            return self.max
        for i, ch in enumerate(self.buf):
            nxt = self.buf[i + 1 : i + 2]
            if ch in ".?!" and (nxt == "" or nxt == " "):
                tail = self.buf[max(0, i - 4) : i + 1].lower()
                if any(tail.endswith(a) for a in ABBREV):
                    continue
                if ch == "." and self.buf[i - 1 : i].isdigit() and nxt.isdigit():
                    continue
                # Ellipsis: a dot that is part of ".." or "..." is not a
                # sentence-final terminal (spec D2: not inside ellipses).
                if ch == "." and (self.buf[i - 1 : i] == "." or nxt == "."):
                    continue
                if nxt == "":
                    return None  # might be mid-token; wait
                return i + 1
            if ch in ",;:" and nxt == " " and i + 1 >= self.min:
                return i + 1
        return None

    def flush(self) -> list[str]:
        out = [self.buf] if self.buf else []
        self.buf = ""
        return out

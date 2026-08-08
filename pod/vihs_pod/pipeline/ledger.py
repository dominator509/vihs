"""INV-1 playback ledger math (SPEC-001 D3/D4).

On ABORT, the committed assistant text = concatenation of fully-played spans
+ a proportional cut of the partially played chunk:

    partial_chars = floor(chars_covered * played_ms / dur_ms)

This is pure arithmetic so `test_abort_inv1.py` can pin it byte-exactly
against fixture timings.
"""

from __future__ import annotations

from dataclasses import dataclass

from .abort_bus import PlayedSpan


@dataclass(frozen=True)
class PartialChunk:
    """The audio chunk that was interrupted mid-playback."""

    clause_id: int
    char_start: int
    chars_covered: int
    dur_ms: int
    played_ms: int

    def committed_chars(self) -> int:
        if self.dur_ms <= 0:
            return 0
        return (self.chars_covered * self.played_ms) // self.dur_ms


def committed_text(
    clauses: dict[int, str],
    played: list[PlayedSpan],
    partial: PartialChunk | None = None,
) -> str:
    """Reconstruct the exact assistant text that was heard.

    `clauses` maps clause_id → full rendered text. Fully played spans are
    concatenated verbatim; a partial chunk contributes its proportional
    prefix.
    """
    parts: list[str] = []
    for span in played:
        clause = clauses.get(span.clause_id, "")
        start = min(span.char_start, len(clause))
        end = min(span.char_end, len(clause))
        if end > start:
            parts.append(clause[start:end])
    if partial is not None:
        clause = clauses.get(partial.clause_id, "")
        start = partial.char_start
        end = start + partial.committed_chars()
        parts.append(clause[start:end])
    return "".join(parts)

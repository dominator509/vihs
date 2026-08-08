"""Clause chunker tests (SPEC-001 D2 required tests).

Property tests: never empty clause, exact concatenation, flush emits residue.
Fixtures: abbreviations, numbers, ellipses, long clauses.
"""

from __future__ import annotations

import random

from vihs_pod.pipeline.clause import ClauseChunker


def feed_all(ch: ClauseChunker, text: str) -> list[str]:
    out: list[str] = []
    for delta in text:
        out.extend(ch.feed(delta))
    out.extend(ch.flush())
    return out


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def test_sentence_final_emits() -> None:
    ch = ClauseChunker()
    assert ch.feed("Hello world.") == []
    assert ch.feed(" How are you?") == ["Hello world."]
    assert ch.feed(" Good.") == [" How are you?"]
    assert ch.flush() == [" Good."]


def test_abbreviation_not_split() -> None:
    ch = ClauseChunker()
    out = feed_all(ch, "Dr. Smith said hello. Then he left.")
    assert out == ["Dr. Smith said hello.", " Then he left."]


def test_number_decimal_not_split() -> None:
    ch = ClauseChunker()
    out = feed_all(ch, "Pi is 3.14 and e is 2.718. That is math.")
    assert len(out) == 2
    assert out[0] == "Pi is 3.14 and e is 2.718."
    assert out[1] == " That is math."


def test_ellipsis_waits() -> None:
    ch = ClauseChunker()
    assert ch.feed("I mean...") == []
    # Ellipsis dots are not terminals; continue until a real sentence end.
    out = ch.feed(" never mind. Done.")
    assert out == ["I mean... never mind."]
    assert ch.flush() == [" Done."]


def test_comma_gates_long_clause_only() -> None:
    ch = ClauseChunker(min_chars=40)
    # Comma before 40 chars: no cut.
    assert ch.feed("Short, but not long enough") == []
    # Comma after 40 chars: cut at comma (fresh chunker, no residue).
    ch2 = ClauseChunker(min_chars=40)
    long = "This is a rather long sentence that goes on, and then continues."
    out = feed_all(ch2, long)
    assert out[0] == "This is a rather long sentence that goes on,"
    assert "".join(out) == long


def test_force_emit_at_max() -> None:
    ch = ClauseChunker(min_chars=40, max_chars=60)
    text = "x" * 120
    out = feed_all(ch, text)
    assert out == ["x" * 60, "x" * 60]


def test_flush_emits_residue() -> None:
    ch = ClauseChunker()
    assert ch.feed("no punctuation at all") == []
    assert ch.flush() == ["no punctuation at all"]


def test_flush_empty_is_empty() -> None:
    ch = ClauseChunker()
    assert ch.feed("") == []
    assert ch.flush() == []


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_prop_no_empty_clauses_and_exact_concatenation() -> None:
    rng = random.Random(0xC1A0E5)
    alphabet = "abcdefghijklmnopqrstuvwxyz .,!?;:0123456789"
    for _ in range(200):
        n = rng.randint(1, 200)
        text = "".join(rng.choice(alphabet) for _ in range(n))
        ch = ClauseChunker()
        out = feed_all(ch, text)
        for c in out:
            assert c != "", "never emit an empty clause"
        assert "".join(out) == text, "concatenation must equal input exactly"


def test_prop_flush_idempotent() -> None:
    rng = random.Random(0xB00B5)
    for _ in range(100):
        ch = ClauseChunker()
        ch.feed(rng.choice(["hello world", "", "a.b.c", "x"]))
        first = ch.flush()
        second = ch.flush()
        assert second == [], "second flush after empty buffer is empty"
        assert all(c.strip() for c in first if c)

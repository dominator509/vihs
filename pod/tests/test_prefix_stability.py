"""Context assembly tests (SPEC-001 D5, INV-4).

Byte-stability: S0||S1||S2 preamble identical across 10 consecutive turns;
changes only at an epoch boundary (a fresh PromptSegments).
"""

from __future__ import annotations

from vihs_pod.context import PromptSegments, TurnLine

S0 = b"system: you are a helpful assistant\n"
S1 = b"persona: calm, warm\n"
S2 = b"# Memory\nuser likes tea.\n"


def make_ctx() -> PromptSegments:
    return PromptSegments(s0_system=S0, s1_persona=S1, s2_memory=S2)


def test_preamble_bytes_identical_across_ten_turns() -> None:
    ctx = make_ctx()
    preambles = set()
    for i in range(10):
        ctx = ctx.with_turn("user", f"turn {i} question")
        ctx = ctx.with_turn("assistant", f"turn {i} answer")
        preambles.add(ctx.preamble())
    assert len(preambles) == 1, "preamble must be byte-identical across 10 turns"
    assert ctx.preamble() == S0 + S1 + S2


def test_preamble_changes_only_at_epoch_boundary() -> None:
    ctx = make_ctx()
    epoch_a = ctx.preamble()
    for _ in range(5):
        ctx = ctx.with_turn("user", "more")
        ctx = ctx.with_turn("assistant", "yes")
    assert ctx.preamble() == epoch_a, "same epoch: preamble unchanged"

    # New epoch = new memory bytes (fresh attach after compaction).
    epoch_b = PromptSegments(
        s0_system=S0,
        s1_persona=S1,
        s2_memory=b"# Memory\nuser likes tea and biscuits.\n",
    )
    assert epoch_b.preamble() != epoch_a, "epoch boundary must change preamble"


def test_render_concatenates_frozen_then_live() -> None:
    ctx = make_ctx()
    ctx = ctx.with_turn("user", "hi")
    ctx = ctx.with_turn("assistant", "hello")
    rendered = ctx.render()
    assert rendered.startswith(S0 + S1 + S2)
    assert rendered.endswith(b"user: hi\nassistant: hello\n")


def test_live_turns_fixed_template() -> None:
    line = TurnLine(role="user", text="hello")
    assert line.render() == b"user: hello\n"


def test_frozen_bytes_not_reserialized() -> None:
    # The same bytes object must be carried through — no re-encoding.
    ctx = make_ctx()
    ctx2 = ctx.with_turn("user", "x")
    assert ctx2.s2_memory is S2
    assert ctx2.s0_system is S0

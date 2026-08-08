"""Context assembly (SPEC-001 D5, INV-4).

Prompt = S0 system core || S1 persona || S2 memory.md || S3 live turns.

S0–S2 are BYTE-frozen at session attach / compaction checkpoint: the pod
stores the exact bytes and reuses them every turn of the epoch. No
re-serialization, no timestamp interpolation, no dict-ordering roulette.
Epoch change arrives ONLY via a new assignment/refresh from memoryd — the
pod never edits S2.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TurnLine:
    """One live turn in the S3 fixed template form."""

    role: str  # "user" | "assistant"
    text: str

    def render(self) -> bytes:
        return f"{self.role}: {self.text}\n".encode()


@dataclass(frozen=True)
class PromptSegments:
    s0_system: bytes
    s1_persona: bytes
    s2_memory: bytes  # FROZEN at attach/epoch
    live: list[TurnLine] = field(default_factory=list)
    voice: str = "default"

    def render(self) -> bytes:
        live_bytes = b"".join(t.render() for t in self.live)
        return self.s0_system + self.s1_persona + self.s2_memory + live_bytes

    def with_turn(self, role: str, text: str) -> PromptSegments:
        """Append a live turn (S3 grows; S0–S2 untouched)."""
        return PromptSegments(
            s0_system=self.s0_system,
            s1_persona=self.s1_persona,
            s2_memory=self.s2_memory,
            live=[*self.live, TurnLine(role=role, text=text)],
            voice=self.voice,
        )

    def preamble(self) -> bytes:
        """The byte-frozen prefix S0||S1||S2 — stability test target."""
        return self.s0_system + self.s1_persona + self.s2_memory

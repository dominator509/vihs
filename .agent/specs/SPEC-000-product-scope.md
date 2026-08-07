# SPEC-000 — Product Scope

Status: accepted · Owner: djw · Roadmap phase: 0 · Linked ExecPlans: EP-000, EP-001

## User-visible goal
A user opens the web client, authenticates, and holds a natural spoken
conversation with a persona avatar: replies begin within ~1.5 s, interruptions
work like they do with a person, and closing the tab loses nothing — "Resume
last session" picks up where they left off, on whatever GPU pod is available.
The operator runs the whole thing on their own infrastructure and watches it
scale itself.

## Non-goals (product)
Multi-party calls; avatar asset authoring; billing (stub interface only);
native mobile; model training/fine-tuning; voice-clone consent workflows;
multi-avatar-per-session.

## Terms
- Session: one durable conversation identified by `session_id`.
- Turn: one user utterance + one avatar response (or its interrupted prefix).
- Puppet: the base visual asset the renderer animates.
- Pod: a disposable GPU compute node running the pipeline.
- Barge-in: user speaking over avatar playback, forcing an abort.
- Endpointing: deciding the user finished their turn.
- Compaction: summarizing old turns into a frozen rolling summary.
- Epoch: the span between compaction checkpoints (preamble is byte-stable
  within it — INV-4).

## Required behavior (system level)
- B1 Conversational loop meets the latency budget (ARCHITECTURE §6).
- B2 Barge-in aborts playback ≤100 ms internally; transcript honors INV-1.
- B3 Sessions are durable and resumable across connections, pods, and
  crashes; the resume greeting continues context naturally.
- B4 Autoscaling: warm-pool floor, preemptive scale at 4/5 fill, cooldown
  drain, hard concurrency cap per pod.
- B5 Export `transcript.md`; hard delete with true removal.
- B6 Every §9 technology-menu row is swappable behind a stage Protocol /
  provider trait without touching invariants.

## Inputs / outputs
In: user audio (Opus over WebRTC), auth token, session commands (create/
resume/delete/export). Out: avatar audio+video track, live captions
(transcript deltas over data channel), transcript.md export.

## Error states
Defined per subsystem in SPEC-006; product-level: pod loss mid-turn degrades
to "reconnecting…" then resume — never a lost conversation.

## Data rules
SPEC-002 governs. Product commitments: events append-only; derived artifacts
regenerable; deletion is real.

## Security rules
SPEC-005 governs. Product commitment: knowing a `session_id` never reveals a
transcript.

## Success metrics
PROJECT_BRIEF.md metrics table is normative.

## Required tests
E2E scripted conversation covering B1–B5 with mock stages (CI) and a staging
run with real stages (EP-009/EP-010).

## Acceptance criteria
All B1–B6 demonstrably true on staging; metrics dashboard shows budget
compliance; chaos pod-kill drill passes.

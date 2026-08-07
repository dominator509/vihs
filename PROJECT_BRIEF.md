# PROJECT_BRIEF — VIHS (Virtual Interaction Hosting Suite)

## Project name
VIHS — Virtual Interaction Hosting Suite.

## Problem statement
Real-time avatar conversation products fail for two reasons: latency (sequential
STT→LLM→TTS→render pipelines land at 4–8 s to first audio and feel broken) and
amnesia (sessions die with the pod that served them). VIHS solves both: a
streamed, clause-pipelined inference pipeline with turn-taking and barge-in that
lands first response in ~0.7–1.3 s, and a pod-independent, hash-chained,
resumable session memory subsystem so a conversation survives disconnects,
crashes, and pod reassignment.

## Target users
- Operators self-hosting an interactive avatar service (privacy-first posture).
- End users conversing with a persona avatar over WebRTC from a browser.
- Downstream coding agents implementing this repository from these blueprints.

## Primary user outcomes
1. User speaks; avatar begins replying (audio + synchronized face) in ≤ 1.5 s
   perceived (target 0.7–1.3 s measured first-chunk critical path).
2. User can interrupt the avatar mid-sentence; playback halts within ~100 ms and
   the avatar returns to a listening pose; the transcript records only audio
   actually heard (INV-1).
3. User disconnects (tab close, network drop, pod crash) and resumes later with
   the avatar recalling the conversation, possibly on a different pod (INV-3).
4. User can export a full human-readable `transcript.md` and hard-delete a
   session (true deletion, not a soft hide).
5. Operator scales GPU pods automatically: warm-pool floor ≥ 1, preemptive
   deploy at 4/5 fill, idle drain after cooldown, load-tested per-GPU cap.

## Business goals
- Self-hostable end-to-end: every layer has an OSS/local option; no mandatory
  SaaS dependency (managed rows in the technology menu are optional swaps).
- GPU cost tracks demand: fixed cost = warm-pool floor only.
- Session durability is a product feature (resume, export, delete).

## Technical goals
- The five core invariants hold at all times (see ARCHITECTURE.md §Invariants):
  INV-1 log-what-was-rendered, INV-2 single-writer, INV-3 pod-independent
  memory, INV-4 prefix stability, INV-5 derived-artifacts-are-cache.
- Per-stage first-chunk latency instrumented and budgeted (SPEC-007).
- Per-GPU concurrency cap is derived by load test, never assumed.
- Chaos-tested crash recovery: kill a pod mid-turn, resume cleanly.

## Out of scope (product level)
- Multi-party group calls (1 user ↔ 1 avatar only in v1).
- Avatar asset authoring tools (base assets are provided inputs).
- Billing/payments (credit/time tracking is a stub interface in v1).
- Native mobile clients (browser WebRTC only).
- Training or fine-tuning models (inference only; weights are inputs).
- Voice cloning consent workflows (operator supplies licensed voices).

## Success metrics
- p50 stop-speaking→first-avatar-audio ≤ 1.3 s; p95 ≤ 2.0 s (loaded pod).
- Barge-in perceived stop ≤ 150 ms p95; abort-flush ≤ 100 ms internal.
- Resume correctness: 100% of committed turns recalled after pod kill.
- Prefix-cache hit rate ≥ 90% on turns between compaction checkpoints.
- Zero torn event-log chains under chaos testing (INV-2 verified by fsck).

## Production readiness definition
Production-ready when every criterion in PRODUCTION_READINESS.md passes,
including: full verification green (`scripts/verify.sh`), chaos pod-kill drill
recovers, capacity cap derived and enforced, security review of §6.5 controls
(auth-gated resume, encryption at rest, scoped pod access, hard delete)
complete, rollback drill executed, runbooks written.

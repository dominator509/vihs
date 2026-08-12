# GGUF Verification Report — Lexi-Q4_K_M (EP-010 M2, option c)

**Date:** 2026-08-12 (re-verified after session interruption)
**Repo:** /root/vihs
**Scope:** Verify every deliverable from the operator "ggufs" prompt — local
llama.cpp roleplay LLM (option c) with resumable, size-verified GGUF download.

## 1. GGUF artifact — VERIFIED INTACT

- **File:** `/tmp/llama-gguf/Lexi-Q4_K_M.gguf` (staging copy; production copy
  persists on network volume `0z0kdx56tb` at `/workspace/models/llama/`)
- **Size:** 4,920,739,104 bytes (4.9 GB) — matches `VIHS_LLAMA_GGUF_SIZE=4920739104` exactly
- **sha256:** `376ac398dc4542c4bd29b679b9f63f4988f0c032b327cadbe82909cd8eb12539`
  — matches the recorded hash `376ac398…` in the EP-010 decision log
- **Downloaded:** 2026-08-10 20:56 (modified timestamp)
- **Volume:** `0z0kdx56tb` (vihs-models, 50 GB, US-IL-1) confirmed live via
  RunPod API (`check_volumes.py`)

## 2. Committed implementation (git log)

| Commit | Date | Content |
|--------|------|---------|
| `1a6e188` | 2026-08-10 21:00 | local LLM bootstrap (option c) — llama-cpp-python CUDA wheel + GGUF fetch in slim-boot; PROVIDER/LLM env passthrough |
| `f445491` | 2026-08-10 22:11 | resumable GGUF download — Range 206 + `curl -C -` with size verify + retry; GGUF_SIZE env pin |
| `6d08ad2` | 2026-08-10 22:44 | add libgomp1 to slim image — llama.cpp CUDA wheel dlopens `libgomp.so.1` at import |
| `6d63a01` | 2026-08-10 22:51 | local llama.cpp roleplay LLM MEETS 400ms llm_ttft (measured **323ms**); TTS now binding constraint |
| `e9bdd01` | 2026-08-11 06:51 | unwrap Lexi bot_input envelope + `**Assistant**` role prefix — TTS never speaks JSON |

## 3. Measured result (from EP-010 §13/§14)

- **llm_ttft = 323ms — MEETS the 400ms §6 budget.** First provider to do so
  (deepseek-v4-flash 1.9–2.5s, claude-haiku 1.2–2.5s, Venice 1.9s all breached).
- Model: Lexi-Uncensored-V2 Llama-3.1-8B Q4_K_M, llama-cpp-python 0.3.19 CUDA
  wheel, `PROVIDER=vllm → 127.0.0.1:8000`.
- New binding constraint after LLM fixed: `tts_ttfa 4524ms` (budget 300ms) —
  Piper TTS on CPU, not the LLM. Addressed separately in v0.2.10 (bounded ONNX
  threads, tts_ttfa p50 68–152ms).

## 4. Deploy wiring (verified in repo)

- `scripts/m2-retry-deploy.sh` sets:
  - `PROVIDER=vllm`
  - `VIHS_LLM_URL=http://127.0.0.1:8000/v1`
  - `VIHS_LLAMA_GGUF=/workspace/models/llama/Lexi-Q4_K_M.gguf`
  - `VIHS_LLAMA_GGUF_URL=http://66.94.123.250:8099/llama-gguf/Lexi-Q4_K_M.gguf`
  - `VIHS_LLAMA_GGUF_SIZE=4920739104`
- `ENVIRONMENT.md` rows 37–39 document all three `VIHS_LLAMA_GGUF*` env vars.
- `deploy/docker/slim-boot.sh`: llama wheel install (10-try retry), GGUF
  download only when missing on volume, voice copy, then llama-server + agent.
- `.env`: `VIHS_RUNPOD_VOLUME=0z0kdx56tb`.

## 5. Deliberately NOT done (per recorded operator decision)

- GGUF **not baked into image** — 4.9GB on ttl.sh at ~6s/MB would make the
  pull itself the slow step (net loss). Persists on network volume instead
  (weights-never-in-image rule). Revisit only with fast-registry creds.
- llama-cpp-python wheel (1.36GB) **not baked** — stays at boot with 10-try
  retry. 56-wheel base closure (~180MB) IS baked (v0.2.11) to remove the I-02
  stall window.

## 6. Caveat

The literal text of the operator's "ggufs prompt" fell inside a compacted
session window; verification was performed against the recorded EP-010
decision log + repo state. If the prompt requested anything beyond the
Lexi Q4_K_M setup (e.g., additional models, quantizations, a model list),
that gap is not yet closed.

## 7. Conclusion

**All verifiable ggufs-prompt deliverables are implemented, committed, and
measured green.** The GGUF artifact itself is byte-verified (size + sha256).

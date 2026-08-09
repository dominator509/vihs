# deploy/runpod — provider templates + volume layout (EP-009 M4)

## Pod template (template JSON)

The VIHS pod template is the RunPod-side contract the provider driver fills at
deploy time (`provider_runpod.rs`). The driver builds the create-pod request
directly (POST /v2/pods) — this file documents the effective template an
operator would see in the RunPod console, kept in sync with the driver's env
injection.

Template: `deploy/runpod/template.json` (image, env, ports, volume mount).

## Network volume layout (VOLUME.md)

RunPod network volume mounted at `VIHS_MODEL_DIR=/workspace/models`
(read-mostly; weights NEVER baked into the image — DEPLOYMENT.md / EP-009 M1).

```
/workspace/models/
├── stt/        # faster-whisper model dir (real STT stage; EP-005 M7)
├── llm/        # vLLM weights / adapter config (real LLM stage)
├── tts/        # Piper TTS voice models (real TTS stage)
├── lipsync/    # lipsync assets (real lipsync stage)
└── assets/     # base avatar assets (client-agnostic)
```

Cold start target 20–30 s (ARCHITECTURE §13) assumes the volume is mounted
and models are resident on the pod host (provider warm-container/snapshot
where supported); first-load downloads would blow the budget.

## Lifecycle rule (operator hard requirement)

Every pod created for testing is TERMINATED when the test ends — the driver's
`terminate` calls `DELETE /v2/pods/{id}` (permanent; a stopped pod keeps
billing). Teardown runs even when the test fails.

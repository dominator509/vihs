# ENVIRONMENT.md — VIHS

## Required tools & versions
| Tool | Version | Used by |
|---|---|---|
| Rust toolchain | 1.79+ (stable, pinned in rust-toolchain.toml) | control plane |
| Python | 3.11.x | pod agent |
| Docker + compose plugin | 24+ | dev services, pod image |
| GStreamer | 1.22+ (pod image only) | mux |
| jq, curl, sh (POSIX) | any recent | scripts |

Package managers: cargo; pip inside `pod/.venv` only. No npm.

## Environment variable registry
Every variable used anywhere MUST have a row here first (AGENTS.md §6).
S=secret, N=non-secret. Env: dev/stage/prod/all.

| Name | Req | Env | Example | S/N | Description | Validation |
|---|---|---|---|---|---|---|
| VIHS_REDIS_URL | yes | all | redis://127.0.0.1:6379 | N | hot index | must connect at preflight of service |
| VIHS_S3_ENDPOINT | yes | all | http://127.0.0.1:9000 | N | object store endpoint | reachable; bucket exists |
| VIHS_S3_BUCKET | yes | all | vihs-sessions | N | sessions bucket | non-empty |
| VIHS_S3_ACCESS_KEY | yes | all | minioadmin | S | store access key | non-empty |
| VIHS_S3_SECRET_KEY | yes | all | minioadmin | S | store secret | non-empty |
| VIHS_MEMORYD_ADDR | yes | all | 127.0.0.1:8091 | N | memoryd bind/target | host:port |
| VIHS_ORCH_ADDR | yes | all | 0.0.0.0:8080 | N | orchestrator bind | host:port |
| VIHS_ADMIN_ADDR | yes | all | 127.0.0.1:8081 | N | admin listener | host:port, non-public |
| VIHS_MCP_ADDR | yes | all | 127.0.0.1:8092 | N | MCP server bind (ADR-011) | host:port |
| VIHS_POD_ADDR | yes | all | 127.0.0.1:8093 | N | pod local bind/advertise (health + signal WS) | host:port reachable from orchestrator |
| VIHS_POD_TOKEN | yes | all | (random 32B b64url) | S | pod bearer for register/health/assign; MUST be 32-byte base64url (seeded at startup) | 32-byte b64url token |
| VIHS_ADMIN_TOKEN | no | dev | (random 32B b64url) | S | bootstrap admin token seeded at startup so POST /admin/tokens is reachable | 32-byte b64url token |
| VIHS_LLM_URL | pod | all | http://127.0.0.1:8000/v1 | N | vLLM OpenAI-compat OR AXIOM gateway base (ADR-012) | reachable in real mode |
| VIHS_LLM_TOKEN | pod | stage,prod | (bearer for AXIOM gateway) | S | brain-stage auth token | non-empty when PROVIDER=axiom-gateway |
| VIHS_LLM_PROVIDER | pod | stage,prod | (gateway registry name) | N | explicit AXIOM gateway provider key (e.g. deepseek, anthropic, openai) | registry name |
| VIHS_LLM_MODEL | pod | stage,prod | (gateway provider default) | N | explicit model id for the AXIOM gateway (e.g. claude-haiku-4-5, gpt-4o-mini) — EP-010 M2 fast-model path | model id the gateway accepts |
| VIHS_LLM_EGRESS | pod | stage,prod | 1 | N | route gateway calls through the model's L2.6 egress sidecar (1) or direct (0) | 0/1 |
| VIHS_LLAMA_GGUF | pod | stage,prod | (unset) | N | local LLM bootstrap: absolute path to a GGUF on the model volume; when set, slim-boot installs llama-cpp-python and starts the OpenAI-compat server on 127.0.0.1:8000 (EP-010 M2 option c) | abs path, e.g. /workspace/models/llama/Lexi-Q4_K_M.gguf |
| VIHS_LLAMA_GGUF_URL | pod | stage,prod | (unset) | N | where slim-boot fetches the GGUF if the volume path is missing (operator mirror preferred) | https/http URL |
| VIHS_LLAMA_GGUF_SIZE | pod | stage,prod | (unset) | N | expected GGUF size in bytes; slim-boot verifies the completed download and restarts on mismatch (EP-010 M2 resume/verify) | int bytes |
| VIHS_TOKEN_PEPPER | yes | all | (random 32B b64url) | S | SHARED token hash pepper -- MUST be identical in orchestrator AND memoryd (EP-006 M3); memoryd refuses to start without it | ≥32 bytes decoded |
| POD_MAX_SESSIONS | yes | all | 2 | N | derived concurrency cap | int ≥1 (ADR-010) |
| SCALE_UP_FILL | no | all | 0.8 | N | preemptive scale threshold (4/5) | 0<x<1 |
| WARM_POOL_FLOOR | yes | stage,prod | 1 | N | pods kept hot 24/7 | int ≥0; ≥1 in prod |
| POD_COOLDOWN_SECS | no | all | 300 | N | idle drain countdown | int ≥60 |
| SESSION_TTL_DAYS | no | all | 90 | N | retention sweep | int ≥1 |
| COMPACT_VERBATIM_TAIL | no | all | 20 | N | turns kept verbatim (§6.4) | int ≥4 |
| COMPACT_TOKEN_BUDGET | no | all | 3000 | N | memory.md token ceiling triggering compaction | int |
| RUNPOD_API_KEY | prod deploy only | prod | (secret) | S | provider driver | present when PROVIDER=runpod |
| PROVIDER | yes | all | mock | N | pod provider driver | mock|runpod |
| VIHS_RUNPOD_IMAGE | no | stage,prod | vihs-pod:latest | N | pod image reference (EP-009 M2) | docker image ref |
| VIHS_RELEASE | no | stage,prod | 0.1.0 | N | release tag the pod registers as versions.pod (EP-009 M5 rollback drill; deploy derives it from the image tag) | semver tag |
| VIHS_RUNPOD_VOLUME_ID | no | stage,prod | (unset) | N | network volume id mounted at VIHS_MODEL_DIR (EP-009 M2) | RunPod volume id |
| VIHS_RUNPOD_REGION | no | stage,prod | (unset) | N | preferred data center id for pod placement (EP-009 M2) | data center id |
| VIHS_RUNPOD_CLOUD | no | stage,prod | SECURE | N | RunPod cloud tier (EP-009 M2) | SECURE\|COMMUNITY\|ANY |
| VIHS_RUNPOD_API_URL | no | test | https://api.runpod.io | N | RunPod API base (fixture override in driver tests; EP-009 M2) | https URL |
| VIHS_RUNPOD_ENV | no | stage,prod | (unset) | N | JSON object merged into pod container env (EP-009 M2) | JSON object |
| VIHS_MODEL_DIR | pod | all | /workspace/models | N | network-volume mount | dir exists on pod |
| VIHS_STT_MODEL / VIHS_STT_DEVICE / VIHS_STT_COMPUTE | no | stage,prod | base / cuda / float16 | N | real STT stage model settings (EP-009 M4; model_dir = $VIHS_MODEL_DIR/stt) | str / cuda\|cpu / float16\|int8 |
| VIHS_TTS_BIN / VIHS_TTS_VOICE / VIHS_TTS_CUDA | no | stage,prod | piper / $VIHS_MODEL_DIR/tts/en_US-lessac-medium.onnx / 0 | N | real TTS stage binary + voice path (EP-009 M4); VIHS_TTS_CUDA=1 enables piper --cuda (EP-010 M2: in-process PiperVoice; model loaded once per pod, ~88ms short clause) | str / path / 0\|1 |
| VIHS_MEMORYD_PUBLIC_ADDR | no | stage,prod | (unset) | N | public addr of memoryd for REMOTE pods (pod MemoryClient talks directly; EP-009 M4) | host:port |
| VIHS_LLM_URL | pod | all | http://127.0.0.1:8000/v1 | N | vLLM OpenAI-compat endpoint | reachable in real mode |
| VIHS_REAL_STAGES | no | dev | 0 | N | 1 = real GPU stages | 0/1 |
| VIHS_MOCK_ANSWERS | no | dev | ["...","..."] | N | scripted mock-LLM answers per turn (E2E) | JSON array of strings |
| VIHS_MOCK_LLM_TTFT_MS / VIHS_MOCK_TTS_TTFA_MS / VIHS_MOCK_LIPSYNC_FF_MS | no | dev | 0 | N | mock-stage latency injection (EP-007 M4 capacity harness CI mode) | ms int |
| VIHS_FAULT | no | dev | (unset) | N | env-gated pod fault hook (EP-007 M5): `stage_crash` wraps the LLM so its first token raises (SPEC-006 row 1 chaos drill) | `stage_crash` |
| VIHS_MODEL_VER | no | all | mock | N | pod metric label `model_ver` (SPEC-007 O1; EP-008 M2) | short string |
| VIHS_MOCK_CACHE_RATIO | no | dev | 0.95 | N | mock LLM prefix-cache hit ratio gauge (SPEC-007 O5; EP-008 M2). Real mode polls VIHS_VLLM_STATS_URL (EP-009) | float 0..1 |
| VIHS_VLLM_STATS_URL | no | prod | (unset) | N | vLLM stats endpoint for the prefix-cache poller (real mode; EP-009) | http URL |
| VIHS_OBS_PROM_PORT | no | dev | 9090 | N | prometheus listen port in the `obs` compose profile (EP-008 M3) | tcp port |
| VIHS_OBS_GRAFANA_PORT | no | dev | 3100 | N | grafana listen port in the `obs` compose profile (EP-008 M3). Default 3100 because 3000 is commonly taken by other host services | tcp port |
| TURN_URL / TURN_USER / TURN_PASS | no | stage,prod | turn:host:3478 | S(creds) | coturn relay | set together |
| RUST_LOG / VIHS_POD_LOG | no | all | info | N | log levels | valid filter |
| VIHS_CLIENT_DIR | no | dev | client/ | N | serve client HTML/JS from this dir instead of the embedded copy (EP-006 M5 dev override) | dir containing index.html + session.js |

Unknown-at-generation vars: none blocking; RUNPOD_API_KEY is STOP S1 only at
EP-009 real-deploy time (mock provider covers everything earlier).

## Local development setup
1. `sh scripts/install.sh` (rust targets, pod venv, lockfile install).
2. `sh scripts/dev-services.sh up` (Redis+MinIO, creates bucket, prints creds).
3. `cp .env.example .env` and fill; `sh scripts/preflight.sh`.
4. Run memoryd, orchestrator, pod `--mock-gpu` (COMMANDS.md rows), open
   `http://localhost:8080/` for the client.

## Test/staging/production environments
- Test = dev services, mock provider, mock stages (CI identical).
- Staging = one real GPU pod on the provider, real stages, WARM_POOL_FLOOR=1,
  separate bucket `vihs-sessions-stage`.
- Production = ENVIRONMENT parity with staging except scale settings and TLS
  termination (reverse proxy) documented in DEPLOYMENT.md.
Parity rule: same binaries/images across stage/prod; only env differs.

## Configuration validation
Each service validates its full env at boot (fail fast, print the offending
var name, never the secret value). `preflight.sh` checks tool versions and
dev-service reachability.

## Troubleshooting
MinIO auth errors → recheck VIHS_S3_* against dev-services output. Pod can't
reach memoryd → VIHS_MEMORYD_ADDR host binding (0.0.0.0 vs 127.0.0.1) in
containers. vLLM TTFT high → confirm `--enable-prefix-caching` and INV-4 test
passing (byte-unstable preamble silently kills the cache).

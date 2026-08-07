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
| VIHS_TOKEN_PEPPER | yes | stage,prod | (random 32B b64) | S | token hash pepper | ≥32 bytes decoded |
| POD_MAX_SESSIONS | yes | all | 2 | N | derived concurrency cap | int ≥1 (ADR-010) |
| SCALE_UP_FILL | no | all | 0.8 | N | preemptive scale threshold (4/5) | 0<x<1 |
| WARM_POOL_FLOOR | yes | stage,prod | 1 | N | pods kept hot 24/7 | int ≥0; ≥1 in prod |
| POD_COOLDOWN_SECS | no | all | 300 | N | idle drain countdown | int ≥60 |
| SESSION_TTL_DAYS | no | all | 90 | N | retention sweep | int ≥1 |
| COMPACT_VERBATIM_TAIL | no | all | 20 | N | turns kept verbatim (§6.4) | int ≥4 |
| COMPACT_TOKEN_BUDGET | no | all | 3000 | N | memory.md token ceiling triggering compaction | int |
| RUNPOD_API_KEY | prod deploy only | prod | (secret) | S | provider driver | present when PROVIDER=runpod |
| PROVIDER | yes | all | mock | N | pod provider driver | mock|runpod |
| VIHS_MODEL_DIR | pod | all | /workspace/models | N | network-volume mount | dir exists on pod |
| VIHS_LLM_URL | pod | all | http://127.0.0.1:8000/v1 | N | vLLM OpenAI-compat endpoint | reachable in real mode |
| VIHS_REAL_STAGES | no | dev | 0 | N | 1 = real GPU stages | 0/1 |
| TURN_URL / TURN_USER / TURN_PASS | no | stage,prod | turn:host:3478 | S(creds) | coturn relay | set together |
| RUST_LOG / VIHS_POD_LOG | no | all | info | N | log levels | valid filter |

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

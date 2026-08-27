# Configuration

SecLLM is configured entirely by **environment variables** (see `src/secllm/config.py`,
`auth.py`, and `audit.py` for the source of truth). There is no config file for the control
plane itself — only the model **catalog** (`models.json`, see the README) is a file.

## Core

| Variable | Default | Meaning |
|---|---|---|
| `SECLLM_HOST` | `0.0.0.0` | control-plane bind address |
| `SECLLM_PORT` | `11400` | control-plane bind port |
| `SECLLM_DATA_DIR` | `./data` | state directory (audit log, etc.) — give each co-located instance its own |
| `SECLLM_ADMIN_TOKEN` | auto-generated | bearer token for the console/control API; printed to the log on boot if unset (also the SSO break-glass credential) |
| `SECLLM_API_TOKEN` | — (open) | bearer token required on `/v1/*` inference routes; unset = open (defense in depth for CUI, e.g. behind SecRouter) |
| `SECLLM_CATALOG` | built-in | path to a `models.json`; unset uses the built-in US-origin catalog |

## Backend & workers

| Variable | Default | Meaning |
|---|---|---|
| `SECLLM_BACKEND` | `vllm` | `vllm` (GPU/CUDA) · `mock` (dev/CI, no GPU) · `mlx` (Apple Silicon, native) · `metal` (Apple Silicon, vLLM-Metal) |
| `SECLLM_WORKER_HOST` | `127.0.0.1` | host each spawned model worker binds to |
| `SECLLM_WORKER_PORT_BASE` | `12000` | first port handed to spawned workers (increments per worker); give each co-located instance its own base |
| `SECLLM_VLLM_ARGS` | — | extra space-separated CLI args appended verbatim to every `vllm serve` invocation |
| `SECLLM_AUTOSTART` | — | comma-separated catalog model ids to load automatically at boot |

## Multi-model GPU scheduling

| Variable | Default | Meaning |
|---|---|---|
| `SECLLM_MAX_LOADED` | `0` | fixed cap on concurrently loaded models; `0` = no fixed cap — models coexist, bounded by real GPU capacity; `>0` restores evict-oldest **switch** semantics (e.g. `1` = one model at a time) |
| `SECLLM_GPU_MEMORY_UTILIZATION` | `0.90` | per-worker VRAM fraction (vLLM `--gpu-memory-utilization`) when a catalog entry sets no `vram_fraction` |
| `SECLLM_GPU_CAP` | `0.95` | max summed VRAM fraction the scheduler will pack onto one GPU, guarding against co-resident models OOMing a card |
| `SECLLM_GPUS` | — (auto) | usable GPU indices, e.g. `0,1,2`; empty auto-detects every GPU `nvidia-smi` reports |

## vLLM-Metal backend (Apple Silicon, `SECLLM_BACKEND=metal`)

| Variable | Default | Meaning |
|---|---|---|
| `SECLLM_METAL_VENV` | `~/.venv-vllm-metal` | path to the external arm64 Python 3.12 venv with vLLM + the vllm-metal plugin installed (run from its own `bin/vllm`, since it can't share SecLLM's interpreter) |
| `SECLLM_METAL_MAX_MODEL_LEN` | `8192` | default `--max-model-len` for metal workers, bounding KV-cache size |
| `SECLLM_METAL_MEM_UTIL` | `0.4` | fallback fraction of unified memory a metal worker reserves when its catalog entry sets no `vram_fraction` |

## Health & reload

| Variable | Default | Meaning |
|---|---|---|
| `SECLLM_HEALTH_INTERVAL` | `10` (s) | health-probe cadence |
| `SECLLM_HEALTH_TIMEOUT` | `5` (s) | per-probe timeout before a failure counts |
| `SECLLM_STARTUP_GRACE` | `600` (s) | time to let a newly-loading model become healthy before it's treated as failed |

## Admin-plane SSO (optional, off by default)

Off by default — the console and control API stay gated by `SECLLM_ADMIN_TOKEN` alone. Set
`SECLLM_OIDC_ISSUER` + `SECLLM_OIDC_AUDIENCE` to accept a SecSSO bearer JWT; add the client
secret + public URL + session secret for the full browser login. See
[security.md](security.md) for the auth model.

| Variable | Default | Meaning |
|---|---|---|
| `SECLLM_OIDC_ISSUER` | — (off) | SecSSO issuer URL; setting this + `_AUDIENCE` turns on admin-plane SSO (bearer) |
| `SECLLM_OIDC_AUDIENCE` | client id | expected token audience; only set explicitly if it differs from `SECLLM_OIDC_CLIENT_ID` |
| `SECLLM_OIDC_CLIENT_ID` | — | confidential client id for the browser (BFF) console login |
| `SECLLM_OIDC_CLIENT_SECRET` | — | confidential client secret for the browser login |
| `SECLLM_PUBLIC_URL` | — | this service's own external URL, used to build the OIDC `redirect_uri` |
| `SECLLM_SESSION_SECRET` | — | HS256 signing secret for the session + OIDC-flow cookies |
| `SECLLM_SESSION_TTL` | `43200` (12h, seconds) | browser session cookie lifetime |
| `SECLLM_ADMIN_GROUP` | `secllm-admins` | SecSSO group a login must carry to administer SecLLM; blank = any authenticated user is an admin |
| `SECLLM_OIDC_JWKS_URL` | — (discovered) | pin the JWKS endpoint instead of OIDC discovery (air-gapped setups without `/.well-known`) |
| `SECLLM_OIDC_AUTHORIZE_URL` | — (discovered) | pin the authorization endpoint instead of OIDC discovery |
| `SECLLM_OIDC_TOKEN_URL` | — (discovered) | pin the token endpoint instead of OIDC discovery |

The static `SECLLM_ADMIN_TOKEN` is always still accepted as a bootstrap / break-glass
credential, regardless of SSO configuration.

## Audit (admin-plane)

| Variable | Default | Meaning |
|---|---|---|
| `SECLLM_AUDIT_ENABLED` | `true` | admin-plane audit log (model load/unload/reload/download, rejected admin attempts); `false`/`0` disables it (every call becomes a no-op) |
| `SECLLM_AUDIT_PATH` | `<data_dir>/audit.jsonl` | hash-chained JSONL audit log path; override to point at an operator-provided protected / SIEM-forwarded volume — see [control-validation.md](control-validation.md) |

## The model catalog

`SECLLM_CATALOG` points at a `models.json` (see `models.example.json`); each entry can set
its own `vram_fraction`, `context_length`, `mlx_model` (MLX-format repo id), `vllm_args`, and
tool-call parser — see the README's catalog table and `src/secllm/catalog.py` for the full
schema.

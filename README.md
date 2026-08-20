# SecLLM — a friendly control plane for vLLM

**Self-hosted inference that a non-expert can actually run.** SecLLM wraps
[vLLM](https://github.com/vllm-project/vllm) with a curated model catalog, one-click
load / unload / **reload**, automatic **health management**, and a clean console — and it
speaks the **OpenAI API**, so SecRouter points at it as a local, in-boundary provider.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Part of the **SecRouter** suite: SecLLM is the on-prem model backend behind the gateway.

## What it does

- **Manages vLLM for you.** Each model runs as a supervised worker; SecLLM starts, stops,
  and reloads them. Several models **coexist**, packed across your GPUs by available VRAM
  (set `SECLLM_MAX_LOADED=1` to instead switch to one model at a time).
- **Health management.** A monitor probes every worker and **auto-restarts** failures
  (bounded), with a startup grace so slow model loads aren't killed prematurely.
- **One OpenAI endpoint.** `POST /v1/chat/completions` (+ `/completions`, `/embeddings`,
  `/v1/models`), routed by model name to the loaded worker — streaming supported. A request
  for a model that isn't loaded gets a clear `404`/`503`, never a hang.
- **A console for humans.** `/admin` — pick a model from the catalog, load/unload/reload,
  **download** weights ahead of time (with live progress), watch health live. No vLLM flags
  to memorize.
- **API-call tracking.** Every `/v1` request is counted per model — requests, errors,
  average latency, and prompt/completion tokens — shown live in the console and served at
  `GET /admin/api/stats`. In-memory only (resets on restart); no request content is stored.
- **US-origin catalog by default.** The shipped models are US-origin open weights (Meta,
  OpenAI gpt-oss), matching the suite's supply-chain posture; edit the catalog to add your own.

## Requirements

- **GPU host (Linux + NVIDIA)** for real inference — vLLM is CUDA-based. The `mock` backend
  runs the whole control plane anywhere (macOS included) for development and CI.

## Quickstart (GPU host)

```bash
docker compose up -d          # SecLLM + a GPU-enabled vLLM backend (see compose.yaml)
# or from source on the host:
uv sync --extra vllm && uv run secllm      # serves on 0.0.0.0:11400
```

Open **http://\<host\>:11400/admin**, paste the admin token (printed on first boot if
`SECLLM_ADMIN_TOKEN` is unset), and **Load** a model. Then point any OpenAI client — or
SecRouter — at `http://\<host\>:11400/v1`.

```bash
curl http://localhost:11400/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Llama-3.1-8B-Instruct","messages":[{"role":"user","content":"hello"}]}'
```

## Wiring SecRouter to SecLLM

Add SecLLM as a local provider and put it in the egress allow-list:

```jsonc
"providers": {
  "secllm": { "api": "openai", "baseUrl": "http://secllm.internal:11400/v1" }
},
"tiers": { "SIMPLE": { "primary": "secllm/Llama-3.2-3B-Instruct" }, "MEDIUM": { "primary": "secllm/Llama-3.1-8B-Instruct" } },
"security": { "egress": { "allowlist": [
  { "provider": "secllm", "allowedHost": "secllm.internal", "authorizedClassifications": ["CUI"] }
] } }
```

SecRouter now routes tiers to on-prem models with the same governance, budgets, and audit as
any other provider.

## The model catalog

`models.example.json` (US-origin defaults). Copy to `models.json`, edit, and set
`SECLLM_CATALOG`:

| id | model | origin | class |
|---|---|---|---|
| `Llama-3.2-3B-Instruct` | Llama 3.2 3B | US (Meta) | small |
| `Llama-3.1-8B-Instruct` | Llama 3.1 8B | US (Meta) | medium |
| `gpt-oss-20b` | gpt-oss-20b | US (OpenAI) | medium |
| `Llama-3.3-70B-Instruct` | Llama 3.3 70B | US (Meta) | large |

Add any model you like — PRC-jurisdiction models (Qwen/DeepSeek/…) are simply excluded from
the shipped defaults, consistent with SecRouter's posture.

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `SECLLM_HOST` / `SECLLM_PORT` | `0.0.0.0` / `11400` | control-plane bind |
| `SECLLM_BACKEND` | `vllm` | `vllm` (GPU) or `mock` (dev/CI) |
| `SECLLM_CATALOG` | built-in | path to a `models.json` |
| `SECLLM_MAX_LOADED` | `0` | fixed cap on loaded models; `0` = GPU-bound coexistence, `>0` = evict-oldest switch (e.g. `1`) |
| `SECLLM_AUTOSTART` | — | comma-separated model ids to load at boot |
| `SECLLM_GPU_MEMORY_UTILIZATION` | `0.90` | per-worker VRAM fraction when a model sets no `vram_fraction` |
| `SECLLM_GPU_CAP` | `0.95` | max summed VRAM fraction the scheduler packs onto one GPU |
| `SECLLM_GPUS` | — (auto) | usable GPU indices, e.g. `0,1,2`; empty auto-detects via `nvidia-smi` |
| `SECLLM_HEALTH_INTERVAL` / `_TIMEOUT` | `10` / `5` | health probe cadence |
| `SECLLM_STARTUP_GRACE` | `600` | seconds to allow a model to load before failing |
| `SECLLM_ADMIN_TOKEN` | auto | bearer token for the console/control API (also the break-glass credential when SSO is on) |
| `SECLLM_API_TOKEN` | — (open) | bearer token required on `/v1/*` inference routes; unset = open (defense in depth for CUI, e.g. behind SecRouter) |
| `SECLLM_OIDC_ISSUER` / `_AUDIENCE` | — (off) | turn on **admin-plane SSO** — a SecSSO bearer/login is accepted for `/admin*` (see below) |
| `SECLLM_OIDC_CLIENT_ID` / `_CLIENT_SECRET` | — | confidential client for the browser (BFF) console login |
| `SECLLM_PUBLIC_URL` / `SECLLM_SESSION_SECRET` | — | this service's external URL + session-cookie signing secret (BFF login) |
| `SECLLM_ADMIN_GROUP` | `secllm-admins` | SecSSO group a login must carry to administer SecLLM; blank = any authenticated user |

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` · `/completions` · `/embeddings` | open*† | OpenAI-compatible inference (routed to the loaded model) |
| `GET /v1/models` | open*† | loaded + healthy models |
| `GET /admin` | open | management console |
| `GET /health` | open | control-plane liveness + loaded models |
| `GET /admin/api/models` | admin | catalog + loaded state + per-model API-call stats |
| `POST /admin/api/models/{id}/{load,unload,reload}` | admin | lifecycle control |
| `POST /admin/api/models/{id}/download` | admin | pre-fetch a model's weights without loading it |
| `GET /admin/api/stats` | admin | per-model + overall API-call counters (requests, errors, latency, tokens) |
| `GET /auth/login` · `/auth/callback` · `POST /auth/logout` · `GET /auth/status` | open‡ | SecSSO admin login (present only when SSO is configured) |

\* Put SecLLM behind SecRouter (or a proxy) for authenticated, governed access — it is an
inference backend, not a public endpoint.

‡ **Admin-plane SSO (optional).** Off by default — the console + control API stay gated by
`SECLLM_ADMIN_TOKEN`, and `/v1` inference keeps its own token (`SECLLM_API_TOKEN`); SecRouter
already governs *client* access to inference, so only the admin plane needs SSO. Set
`SECLLM_OIDC_ISSUER` + `SECLLM_OIDC_AUDIENCE` and the console accepts a SecSSO **login** (browser,
PKCE, httpOnly session cookie — add the client id/secret + `SECLLM_PUBLIC_URL` +
`SECLLM_SESSION_SECRET`) or a **bearer JWT** (CLI/scripts, verified against SecSSO's JWKS). Either
must also be a member of `SECLLM_ADMIN_GROUP` (default `secllm-admins`). The static
`SECLLM_ADMIN_TOKEN` is always still accepted as a bootstrap / break-glass credential.

† Set `SECLLM_API_TOKEN` to require `Authorization: Bearer <token>` on all `/v1/*` routes
instead (defense in depth on top of network isolation — useful when serving CUI). `GET /health`
is never gated, since it's used for liveness/monitoring/SecRouter's circuit breaker. Leave it
unset and `/v1` stays open, as above.

## Running multiple instances

SecLLM instances are stateless and don't coordinate with each other — run as many as
you like side by side and point SecRouter at all of them as one provider, using a
`baseUrl` array (one entry per instance, each identified by its `host:port`) instead
of a single string:

```jsonc
"providers": {
  "secllm": {
    "api": "openai",
    "baseUrl": [
      "http://secllm-1.internal:11400/v1",
      "http://secllm-2.internal:11400/v1",
      "http://secllm-3.internal:11400/v1"
    ]
  }
}
```

SecRouter round-robins requests across the list and skips, via its per-endpoint
circuit breaker, any instance with recent connection/5xx/timeout failures — a dead
instance stops getting traffic without operator intervention.

Instances can run the **same** model (extra capacity — any of them can answer) or
**different** models (a partitioned catalog — e.g. `Llama-3.2-3B-Instruct` on one box, `Llama-3.3-70B-Instruct` on
another). SecRouter learns which models each instance is currently serving by
polling every instance's `GET /v1/models`, and routes a request only to the
instances that actually serve the requested model, round-robining across those
(**model-aware** load balancing). Before the first poll — when nothing is known yet
— it still tries an instance and treats a `404` (`model_not_loaded`) as an ordinary
client error, not a health failure, falling through to the next instance; a `503`
(worker present but unhealthy) *does* count toward the per-endpoint circuit breaker
and can eventually take a genuinely broken instance out of rotation.

When deployed by SecDeploy, this pool (the `baseUrl` array, the bearer token, and the
egress authorization) is generated for you from the site topology — you don't hand-write
the SecRouter config; see SecDeploy's multi-instance-inference docs.

**Co-locating instances on one host:** give each its own `SECLLM_PORT`,
`SECLLM_DATA_DIR`, and `SECLLM_WORKER_PORT_BASE` — the defaults (`11400` / `./data`
/ `12000`, see Configuration above) collide if two instances share a host. Across
separate hosts, the defaults are fine unchanged.

Each instance's `GET /health` (liveness + `loaded[].state` per worker) and
`GET /v1/models` (the currently-healthy served set) — both above — are the signals
to point per-instance monitoring at.

**Auth to the pool:** if the instances have `SECLLM_API_TOKEN` set, SecRouter sends it as
the bearer token on every request to the `secllm` provider (all instances in the pool must
share the same token). On the SecRouter side this is configured via `SECROUTER_SECLLM_TOKEN`.

## License

[Apache 2.0](LICENSE) — Copyright 2026 Austin Probe. vLLM and model weights are third-party
(see [NOTICE](NOTICE)).

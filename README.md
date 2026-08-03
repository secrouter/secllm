# SecLLM — a friendly control plane for vLLM

**Self-hosted inference that a non-expert can actually run.** SecLLM wraps
[vLLM](https://github.com/vllm-project/vllm) with a curated model catalog, one-click
load / unload / **reload**, automatic **health management**, and a clean console — and it
speaks the **OpenAI API**, so SecRouter points at it as a local, in-boundary provider.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Part of the **SecRouter** suite: SecLLM is the on-prem model backend behind the gateway.

## What it does

- **Manages vLLM for you.** Each model runs as a supervised worker; SecLLM starts, stops,
  and reloads them. On a single GPU, loading a new model **switches** to it.
- **Health management.** A monitor probes every worker and **auto-restarts** failures
  (bounded), with a startup grace so slow model loads aren't killed prematurely.
- **One OpenAI endpoint.** `POST /v1/chat/completions` (+ `/completions`, `/embeddings`,
  `/v1/models`), routed by model name to the loaded worker — streaming supported. A request
  for a model that isn't loaded gets a clear `404`/`503`, never a hang.
- **A console for humans.** `/admin` — pick a model from the catalog, load/unload/reload,
  watch health live. No vLLM flags to memorize.
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
  -d '{"model":"balanced","messages":[{"role":"user","content":"hello"}]}'
```

## Wiring SecRouter to SecLLM

Add SecLLM as a local provider and put it in the egress allow-list:

```jsonc
"providers": {
  "secllm": { "api": "openai", "baseUrl": "http://secllm.internal:11400/v1" }
},
"tiers": { "SIMPLE": { "primary": "secllm/fast" }, "MEDIUM": { "primary": "secllm/balanced" } },
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
| `fast` | Llama 3.2 3B | US (Meta) | small |
| `balanced` | Llama 3.1 8B | US (Meta) | medium |
| `reasoning` | gpt-oss-20b | US (OpenAI) | medium |
| `large` | Llama 3.3 70B | US (Meta) | large |

Add any model you like — PRC-jurisdiction models (Qwen/DeepSeek/…) are simply excluded from
the shipped defaults, consistent with SecRouter's posture.

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `SECLLM_HOST` / `SECLLM_PORT` | `0.0.0.0` / `11400` | control-plane bind |
| `SECLLM_BACKEND` | `vllm` | `vllm` (GPU) or `mock` (dev/CI) |
| `SECLLM_CATALOG` | built-in | path to a `models.json` |
| `SECLLM_MAX_LOADED` | `1` | concurrent loaded models (raise for multi-GPU) |
| `SECLLM_AUTOSTART` | — | comma-separated model ids to load at boot |
| `SECLLM_GPU_MEMORY_UTILIZATION` | `0.90` | passed to vLLM |
| `SECLLM_HEALTH_INTERVAL` / `_TIMEOUT` | `10` / `5` | health probe cadence |
| `SECLLM_STARTUP_GRACE` | `600` | seconds to allow a model to load before failing |
| `SECLLM_ADMIN_TOKEN` | auto | bearer token for the console/control API |

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` · `/completions` · `/embeddings` | open* | OpenAI-compatible inference (routed to the loaded model) |
| `GET /v1/models` | open* | loaded + healthy models |
| `GET /admin` | open | management console |
| `GET /health` | open | control-plane liveness + loaded models |
| `GET /admin/api/models` | admin | catalog + loaded state |
| `POST /admin/api/models/{id}/{load,unload,reload}` | admin | lifecycle control |

\* Put SecLLM behind SecRouter (or a proxy) for authenticated, governed access — it is an
inference backend, not a public endpoint.

## License

[Apache 2.0](LICENSE) — Copyright 2026 Austin Probe. vLLM and model weights are third-party
(see [NOTICE](NOTICE)).

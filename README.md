# SecLLM — a friendly control plane for vLLM

**Self-hosted inference that a non-expert can actually run.** SecLLM wraps
[vLLM](https://github.com/vllm-project/vllm) with a curated model catalog, one-click
load / unload / **reload**, automatic **health management**, and a clean console — and it
speaks the **OpenAI API**, so [SecRouter](https://github.com/secrouter/secrouter) points at it
as a local, in-boundary provider. Part of the
[SecRouter suite](https://github.com/secrouter/secdeploy#the-suite).

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

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
- **Audited admin plane.** Model load/unload/download and rejected admin attempts are
  written to a tamper-evident, hash-chained log, with a one-shot CMMC evidence bundle — see
  [Control Validation](docs/control-validation.md).
- **US-origin catalog by default.** The shipped models are US-origin open weights (Meta,
  OpenAI gpt-oss), matching the suite's supply-chain posture; edit the catalog to add your own.
- **Managed catalog, not just a file.** Hot-reload (`POST /admin/api/catalog/reload`) and
  full CRUD (`GET`/`PUT`/`DELETE /admin/api/catalog/models/{id}`) from the console or API —
  admin-gated, schema-validated, write-through (atomic, diff-friendly) to `models.json`, and
  audited. Models already loaded keep running through a reload; only future loads see the new
  entries. Optionally pin a model to an exact HF commit/tag (`revision`) for reproducible,
  supply-chain-reviewed weights — the model analogue of `suite.toml` pinning.

## Requirements

- **GPU host (Linux + NVIDIA)** for real inference — vLLM is CUDA-based. The `mock` backend
  runs the whole control plane anywhere (macOS included) for development and CI. Apple
  Silicon has two native GPU-accelerated backends of its own — see
  [docs/configuration.md](docs/configuration.md).

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

See [docs/deploy.md](docs/deploy.md) for Docker/native/systemd deployment and a production
checklist.

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
any other provider. Running several SecLLM instances as one pool, or serving a partitioned
catalog across them? See [docs/deploy.md](docs/deploy.md#running-multiple-instances).

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

Manage the catalog live instead of hand-editing the file: the console's **Catalog** card (or
`GET`/`PUT`/`DELETE /admin/api/catalog/models/{id}`) adds, edits, and removes entries with
schema validation and an audited write-through to `models.json`; `POST
/admin/api/catalog/reload` re-reads the file and hot-swaps it in without disturbing already-
loaded models. The built-in catalog is read-only (mutations 409 — set `SECLLM_CATALOG` first).
Pin a model to an exact Hugging Face commit or tag with `revision` for reproducible,
supply-chain-reviewed weights (the model-weights analogue of `suite.toml` pinning) — see
[docs/configuration.md](docs/configuration.md#the-model-catalog).

## Documentation

- [docs/index.md](docs/index.md) — start here
- [docs/deploy.md](docs/deploy.md) — Docker / native / systemd, multi-GPU scheduling, running
  multiple instances, production checklist
- [docs/configuration.md](docs/configuration.md) — every environment variable
- [docs/usage.md](docs/usage.md) — the OpenAI-compatible API, the admin API, and a console tour
- [docs/security.md](docs/security.md) — the two auth surfaces, admin-plane SSO, audit log,
  FIPS posture
- [docs/control-validation.md](docs/control-validation.md) — CMMC / NIST 800-171 control mapping

## License

[Apache 2.0](LICENSE) — Copyright 2026 Austin Probe. vLLM and model weights are third-party
(see [NOTICE](NOTICE)).

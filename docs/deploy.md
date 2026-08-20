# Deploying SecLLM

SecLLM's control plane is light; the weight is vLLM + the models, which need a GPU.

## GPU host (Docker)

Prereqs: Linux + NVIDIA GPU, driver, and the **NVIDIA Container Toolkit**.

```bash
export SECLLM_ADMIN_TOKEN=$(openssl rand -hex 24)
export HUGGING_FACE_HUB_TOKEN=hf_...        # required for gated models (Llama)
docker compose up -d --build
```

Then open `http://<host>:11400/admin`, **Load** a model (the first load downloads weights —
minutes), and point SecRouter at `http://<host>:11400/v1`.

Preload at boot instead of via the console:

```bash
SECLLM_AUTOSTART=gemma-4-26B-A4B-it docker compose up -d
```

## GPU host (native, no container)

On a host with a working vLLM install (part of the SecDeploy `fedora-fips` native path):

```bash
uv sync --extra vllm
SECLLM_ADMIN_TOKEN=… HUGGING_FACE_HUB_TOKEN=… uv run secllm
```

SecDeploy installs SecLLM as a hardened systemd unit on the Fedora target; SecLLM spawns
`vllm serve` workers under its own supervision.

## Multiple models at once

By default several models run at once, bounded by real GPU capacity rather than a fixed count.
The scheduler packs each worker onto the least-loaded GPU(s) that still have room, pins it with
`CUDA_VISIBLE_DEVICES`, and hands vLLM the matching `--gpu-memory-utilization` — so two models
share a big card without either OOMing it, and a tensor-parallel model spreads across several.
When the GPUs are full a Load is refused (HTTP 409) instead of crowding a card.

- `SECLLM_GPU_CAP` (default `0.95`) — the most summed VRAM fraction the scheduler will pack
  onto one GPU.
- `SECLLM_GPUS` (e.g. `0,1,2`) — restrict the usable GPUs; empty auto-detects every card
  `nvidia-smi` reports.
- Per-model `vram_fraction` in the catalog — how much of one GPU a model reserves (falls back
  to `SECLLM_GPU_MEMORY_UTILIZATION` when unset).
- `SECLLM_MAX_LOADED` (default `0` = no fixed cap, GPU-bound). Set it to a positive number to
  restore a hard ceiling with evict-oldest **switch** semantics — e.g. `1` makes loading a new
  model replace the current one, the old single-GPU behaviour.

On a host without a usable `nvidia-smi` (or the mock backend) there's no inventory to manage, so
workers launch unmanaged exactly as before and `SECLLM_MAX_LOADED` is the only bound.

## Health & reload

The monitor probes each worker on `SECLLM_HEALTH_INTERVAL` and auto-restarts a worker that
fails `SECLLM_HEALTH_TIMEOUT` probes (bounded to 5 restarts, then `error`). A model still
loading is protected by `SECLLM_STARTUP_GRACE` (default 600 s) so a slow first load isn't
mistaken for a failure. Reload a model by hand from the console or
`POST /admin/api/models/<id>/reload`.

## Behind SecRouter

Don't expose SecLLM's `/v1` directly to users — it has no per-user auth. Put it in SecRouter's
egress allow-list and let the gateway handle OIDC, policy, budgets, and audit. See the README
for the provider + egress snippet.

By default `/v1` is open, relying on network isolation (SecLLM sitting behind SecRouter / not
publicly reachable). For defense in depth — e.g. when serving CUI — set `SECLLM_API_TOKEN` to
require `Authorization: Bearer <token>` on every `/v1/*` request:

```bash
export SECLLM_API_TOKEN=$(openssl rand -hex 24)
```

Configure the matching token on the SecRouter side via `SECROUTER_SECLLM_TOKEN` so it's sent
as the bearer auth for the `secllm` provider (same token across every instance in a pool — see
"Running multiple instances" in the README). `GET /health` is never gated by this token; it
stays open for liveness/monitoring/SecRouter's circuit breaker probes.

## Models & licenses

Weights are downloaded at load time from Hugging Face under their own licenses; accept the
model's terms on HF and provide `HUGGING_FACE_HUB_TOKEN` for gated repos. The default catalog
is US-origin open weights (Meta, OpenAI gpt-oss); edit `models.json` to change it.

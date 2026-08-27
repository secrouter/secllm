# Changelog

## [Unreleased]

### Compliance
- **Admin-plane audit log.** Every admin-plane mutation (`model.load`, `model.unload`,
  `model.reload`, `model.download` started/completed/failed) and every rejected admin
  attempt (`auth.failure`) is now written to an append-only, SHA-256 hash-chained JSONL log
  — `GET /admin/api/audit` to read it, `GET /admin/api/audit/verify` to check the chain for
  tampering. Configurable via `SECLLM_AUDIT_ENABLED` / `SECLLM_AUDIT_PATH`.
- **CMMC evidence bundle.** `GET /admin/api/evidence` returns a one-shot JSON document —
  sanitized config, the audit-chain verification result, the recent audit trail, and a
  control self-assessment — suitable evidence for an SSP package. See
  [docs/control-validation.md](docs/control-validation.md) for the full NIST SP 800-171 /
  CMMC L2 control mapping.

### Admin console
- **Field-console restyle.** The console now shares the SecRouter suite's visual identity
  (warm manila-paper light theme, "night ops" dark theme, suite logo mark) instead of its
  original bespoke dark theme.
- **Light/dark toggle**, following the OS theme by default with the choice persisted in the
  browser.

## [1.0.0]

First tracked release of **SecLLM** — a self-hosted, OpenAI-compatible control plane for
vLLM. Apache 2.0.

### Serving & models
- **Real model names.** Models are served and requested by their actual name (e.g.
  `Llama-3.1-8B-Instruct`) instead of generic tier tags (`fast`/`balanced`/`large`).
- **Native MLX backend** for Apple Silicon, alongside the CUDA/vLLM and `mock` (dev/CI)
  backends.
- **vLLM-Metal backend** (`SECLLM_BACKEND=metal`) — the full vLLM engine on Apple Silicon
  via an external vllm-metal venv, loading the same MLX-format quants as the `mlx` backend.
- **Per-load context-length override** and a per-model catalog default.
- **Per-model sampling overrides** in the catalog (e.g. defaulting Gemma 4 to greedy
  decoding) instead of one global sampling policy.
- **Server-side tool-calling** (per-model `--tool-call-parser`) on the vLLM and metal
  backends, surfaced in the console.
- **Agent-eval hardening** — a sane default context length for metal workers, correct usage
  reporting on streaming responses, and a `max_model_len` field on `GET /v1/models` so
  clients (e.g. agent preflight checks) can size their token budgets against what the server
  will actually accept instead of discovering the cap mid-session.

### Multi-model GPU scheduling
- **Concurrent multi-model serving** — several models coexist by default, bounded by real
  GPU capacity (`SECLLM_GPU_CAP`, `SECLLM_GPUS`) rather than a fixed count; `SECLLM_MAX_LOADED`
  restores the original evict-oldest single-model behavior for hosts that want it.
- **Per-model VRAM sizing** (`vram_fraction` in the catalog) for both the vLLM
  `--gpu-memory-utilization` packing scheduler and the vLLM-Metal unified-memory reservation.
- A **Load is refused (`409`)** rather than crowding a GPU when there's no room left.

### Admin console & operations
- **Download button**, decoupled from Load, to pre-fetch a model's weights without also
  starting a worker.
- **Live download progress** — transfer rate and ETA, computed from the on-disk HF cache —
  and a fix for progress reporting over 100% from double-counted partial blobs.
- **API-call tracking** — per-model requests, errors, average latency, and
  prompt/completion tokens, in memory, served at `GET /admin/api/stats`.

### Security
- **Optional bearer-token auth** (`SECLLM_API_TOKEN`) on the `/v1` inference API, for
  defense in depth when SecLLM can't rely solely on network isolation.
- **Optional admin-plane SSO** (`SECLLM_OIDC_*`) — a SecSSO bearer JWT or full PKCE browser
  login, gated further by admin-group membership, with the static `SECLLM_ADMIN_TOKEN`
  always available as a bootstrap / break-glass credential.

### Credits
Built from scratch as the on-prem inference backend for the SecRouter suite.

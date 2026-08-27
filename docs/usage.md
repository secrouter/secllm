# Usage

## OpenAI-compatible inference

SecLLM speaks the OpenAI API on `/v1`, so any OpenAI client (or SecRouter, pointed at it as a
provider) can use it unchanged.

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | open\* | chat completions, routed to the loaded worker for `model`; streaming supported (`"stream": true`) |
| `POST /v1/completions` | open\* | legacy text completions |
| `POST /v1/embeddings` | open\* | embeddings |
| `GET /v1/models` | open\* | the currently loaded **and healthy** models, OpenAI's `list` shape, plus a `max_model_len` field mirroring vLLM's own extension |

A request naming a model that isn't loaded gets a clear `404` (`model_not_loaded` /
`model_not_found`) instead of a hang; a request to a worker that's unhealthy gets `503`
(`model_unavailable`). Every `/v1` call is counted (requests, errors, average latency,
prompt/completion tokens) per model, in memory — see `GET /admin/api/stats` below.

\* Set `SECLLM_API_TOKEN` to require `Authorization: Bearer <token>` on all `/v1/*` routes
instead — see [security.md](security.md). `GET /health` is never gated.

## Admin API

All `/admin/api/*` routes require the admin token or a SecSSO admin login (see
[security.md](security.md)).

| Endpoint | Purpose |
|---|---|
| `GET /admin/api/models` | catalog + loaded state, per-model download/cache status, and API-call stats |
| `POST /admin/api/models/{id}/load` | start a worker for `{id}` (optional `{"context_length": N}` body to override the catalog default for this load) |
| `POST /admin/api/models/{id}/unload` | stop `{id}`'s worker |
| `POST /admin/api/models/{id}/reload` | restart `{id}`'s worker (same context-length override option as load) |
| `POST /admin/api/models/{id}/download` | pre-fetch `{id}`'s weights without loading/serving it |
| `GET /admin/api/stats` | per-model + overall API-call counters (requests, errors, latency, tokens) |
| `GET /admin/api/audit` | recent admin-plane audit events, newest first (`?type=model.load`, `?limit=N`, capped at 1000) |
| `GET /admin/api/audit/verify` | tamper-evidence check on the audit hash chain — `{ok, checked, brokenAtSeq?}` |
| `GET /admin/api/evidence` | one-shot CMMC evidence bundle: sanitized config, audit-chain verification, recent audit trail, control self-assessment (downloads as `secllm-evidence-<date>.json`) |

`GET /health` (open, ungated) reports control-plane liveness, the loaded models and their
state, and the admin-plane auth posture — the signal for liveness probes and SecRouter's
circuit breaker.

See [control-validation.md](control-validation.md) for how the audit log and evidence bundle
map to specific CMMC/NIST 800-171 controls.

## Admin console tour

`GET /admin` serves a single self-contained HTML page (no external assets — everything
inline, consistent with the suite's air-gap posture). Sign in with either an OIDC SecSSO
login (when configured) or the admin token, then:

- **Models list** — every catalog entry, whether it's loaded, its health state, and its
  local cache/download status (with a live progress bar, transfer rate, and ETA while a
  download is in flight).
- **Load / Reload** — start or restart a model's worker; an optional context-length field
  overrides the catalog's default context window for that load only (blank = catalog
  default). Several models load and serve concurrently, packed across your GPUs by available
  VRAM — a Load is refused (`409`) rather than crowding a card when there isn't room. Set
  `SECLLM_MAX_LOADED=1` to instead have Load evict the currently-loaded model (the old
  single-model behavior).
- **Download** — pre-fetch a model's weights without loading/serving it, decoupled from
  Load, so you can warm several models ahead of time; Load downloads automatically too if
  you skip this.
- **Unload** — stop a model's worker.
- **Theme toggle** — a light/dark switch (top-right) in the shared SecRouter-suite "field
  console" style; it follows the OS theme by default and the choice persists in the
  browser's `localStorage`.

The console polls `GET /admin/api/models` and `GET /admin/api/stats` to stay live, and calls
the load/unload/reload/download routes above for every action a human takes.

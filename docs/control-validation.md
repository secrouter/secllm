# SecLLM — Control Validation

Maps SecLLM's **admin plane** to selected **NIST SP 800-171 Rev 2** requirements (CMMC L2).
SecLLM is a model-serving control plane, not a client-facing gateway — this doc is honest about
what it does and does not own. See [Shared Responsibility](#shared-responsibility) for the
rest, and `docs/deploy.md` for the deployment topology this assumes.

**Scope note (read this first):** SecLLM's `/v1/*` inference path is deliberately **not**
audited or access-controlled at the per-user level here — that governance (OIDC identity,
policy, budgets, egress control, per-request audit) is delegated to **SecRouter**, which sits in
front of SecLLM and is the suite's reference audit implementation. SecLLM's own controls below
cover only the **admin plane**: `/admin` (console) and `/admin/api/*` (model load/unload/reload/
download, stats, audit, evidence). See `docs/deploy.md:68` ("Behind SecRouter": *"Put it in
SecRouter's egress allow-list and let the gateway handle OIDC, policy, budgets, and audit"*).

Status legend: ✅ enforced in code · 🤝 shared (needs enclave/process) · ⚠️ gap.

## AU — Audit & Accountability

| Family | ID | Requirement | Implementation (file:function) | Evidence command |
|---|---|---|---|---|
| AU | 3.3.1 | Create and retain audit records for admin-plane events | `src/secllm/audit.py` (`AuditLogger.record`); wired at every mutation route in `src/secllm/admin/api.py` — `model.load`, `model.unload`, `model.reload`, `model.download` (started/completed/failed), `auth.failure` | `curl -H "Authorization: Bearer $TOKEN" $URL/admin/api/audit?limit=50` |
| AU | 3.3.2 | Trace audit records to an individual | Every record's `principal` is the OIDC `sub` (SecSSO admin) or the literal `"token-admin"` for the static break-glass credential — never anonymous for a mutation | `admin/api.py` `require_admin` (returns the principal used at every call site) |
| AU | 3.3.7 | Authoritative, time-synced timestamps | UTC ISO-8601 `ts` on every record (host NTP — 🤝) | `src/secllm/audit.py` `_utc_now_iso` |
| AU | 3.3.8 | **Protect audit information** (tamper-evidence) | SHA-256 hash chain over each record's own canonicalized (sorted-keys JSON) fields; genesis `"GENESIS"`; `0700`/`0600` at-rest permissions on the log dir/file | `src/secllm/audit.py` `verify_chain`; `curl .../admin/api/audit/verify` → `{ok, checked, brokenAtSeq?}` |
| AU | 3.3.9 | Limit audit management to authorized users | `/admin/api/audit*` and `/admin/api/evidence` are admin-gated exactly like every other `/admin/api/*` route | `admin/api.py` `require_admin` on each route |
| — | CUI-safe logging | Audit records are metadata only — model ids, byte counts, error strings, config keys | never prompt/response/weights content, never a token value | `src/secllm/audit.py` module docstring; `_sanitized_config` in `admin/api.py` |

## IA — Identification & Authentication

| Family | ID | Requirement | Implementation (file:function) | Evidence command |
|---|---|---|---|---|
| IA | 3.5.1 / 3.5.2 | Authenticate the identity of users before granting admin access | SecSSO OIDC: bearer JWT (RS256, verified against JWKS: issuer/audience/expiry) or a PKCE browser login (httpOnly session cookie); gated further by admin-group membership | `src/secllm/auth.py` (`verify_bearer`, `_verify_id_token`, `is_admin`) |
| IA | — | Break-glass / air-gapped fallback | A static `SECLLM_ADMIN_TOKEN` is always accepted (the *only* credential when SSO is off); every use is audited with `principal="token-admin"`, distinguishing it from a real OIDC identity in the trail | `src/secllm/admin/api.py` `require_admin`; audit records tagged accordingly |
| IA | 3.5.4 | Replay-resistant authentication | Short-lived signed JWTs (bearer) / session cookies with `exp`; PKCE (S256) on the browser flow; disjoint `iss`/`aud` between the session and OIDC-flow cookies so one can't be replayed as the other | `src/secllm/auth.py` |

## CM — Configuration Management

| Family | ID | Requirement | Implementation (file:function) | Evidence command |
|---|---|---|---|---|
| CM | 3.4.6 | Least functionality | `/v1/*` carries no admin capability; admin mutation routes are a small, fixed, documented set (load/unload/reload/download) — no arbitrary config-write endpoint exists | `src/secllm/admin/api.py` `build_router` |

## Evidence bundle

`GET /admin/api/evidence` (admin-gated) returns one JSON document combining a sanitized config
snapshot (catalog, backend, paths — **never** `SECLLM_ADMIN_TOKEN`, `SECLLM_API_TOKEN`, or any
OIDC client/session secret; only whether one is configured), the audit-chain verification
result, the last 200 audit records, and this control self-assessment — suitable evidence for an
SSP package. Named `secllm-evidence-<date>.json`.

## Shared Responsibility

SecLLM enforces the admin-plane controls above. The accreditation boundary must also provide:

- **Inference-path governance (AC, AU, all of it)** — per-user auth, policy, model/tier
  allow-lists, egress control, per-request audit, and budgets are **SecRouter's job**, not
  SecLLM's. Do not expose `SECLLM_API_TOKEN`-protected `/v1` directly to end users; put SecLLM
  behind SecRouter as documented in `docs/deploy.md`. Auditing an inference request here would
  duplicate (and risk drifting from) SecRouter's own CUI-safe audit trail — see SecRouter's
  `docs/compliance/cmmc-control-matrix.md`.
- **Enclave / network** (3.13.1) — SecLLM binds to whatever host/port it's given; network
  isolation (private subnet, firewall) is the deployment's responsibility.
- **FIPS-validated cryptography** (3.13.11) — the linked OpenSSL / Python crypto stack.
- **IdP** (3.5.3 MFA) — SecSSO / the upstream OIDC provider enforces MFA; SecLLM only verifies
  what it's handed.
- **NTP** (3.3.7), **log retention/review**, **SIEM forwarding** (point a log shipper at the
  audit JSONL path), **vulnerability management**, **incident response**, **at-rest volume
  encryption** for the data directory.

## Verification

```bash
# Recent admin-plane audit events
curl -H "Authorization: Bearer $SECLLM_ADMIN_TOKEN" http://localhost:11400/admin/api/audit

# Tamper-evidence check
curl -H "Authorization: Bearer $SECLLM_ADMIN_TOKEN" http://localhost:11400/admin/api/audit/verify

# Full evidence bundle for an SSP package
curl -H "Authorization: Bearer $SECLLM_ADMIN_TOKEN" http://localhost:11400/admin/api/evidence \
  -o secllm-evidence-$(date +%F).json
```

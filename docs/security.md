# Security

## Two independent gates

SecLLM has two separate, independently-configured auth surfaces — they answer different
questions and neither implies the other:

- **`/v1/*` (inference)** — "who may run a completion." Open by default (SecLLM is meant to
  sit behind SecRouter or another network boundary, not be exposed to end users directly).
  Set `SECLLM_API_TOKEN` to require `Authorization: Bearer <token>` on every `/v1/*` request
  as defense in depth (e.g. when serving CUI). `GET /health` and `GET /v1/models` are never
  gated by this token beyond the same bearer check as other `/v1` routes (health is never
  gated at all — it's the liveness/circuit-breaker signal).
- **`/admin` + `/admin/api/*` (admin plane)** — "who may load/unload/reload/download models,
  and see stats/audit/evidence." Gated by `SECLLM_ADMIN_TOKEN` alone by default, optionally
  upgraded to SecSSO OIDC.

## Admin-plane authentication

Off by default: with no `SECLLM_OIDC_*` set, the only credential is the static
`SECLLM_ADMIN_TOKEN` (auto-generated and logged on first boot if you don't set one).

Set `SECLLM_OIDC_ISSUER` + `SECLLM_OIDC_AUDIENCE` to additionally accept a **SecSSO bearer
JWT** — verified RS256 against the issuer's published JWKS (issuer, audience, expiry; never
an attacker-forgeable algorithm). Add `SECLLM_OIDC_CLIENT_ID` / `_CLIENT_SECRET` +
`SECLLM_PUBLIC_URL` + `SECLLM_SESSION_SECRET` to also enable the **browser login**: an
Authorization Code + PKCE (S256) flow run entirely server-side (BFF pattern) — the browser's
only credential is an httpOnly, `SameSite=Lax` session cookie, signed HS256 with its own
`iss`/`aud` disjoint from the short-lived OIDC-flow cookie (so one can never be replayed as
the other). See `SECLLM_SESSION_TTL` for the session lifetime.

Either path still requires the caller to be a member of `SECLLM_ADMIN_GROUP` (default
`secllm-admins`) — a valid SecSSO login is necessary but not sufficient to administer
SecLLM. Set it blank to let any authenticated SecSSO user administer instead.

The static `SECLLM_ADMIN_TOKEN` is **always** still accepted, even with SSO configured — the
bootstrap / break-glass credential for an air-gapped or pre-SSO deployment. Every use is
audited with `principal="token-admin"`, distinguishing it in the trail from a real OIDC
identity (`sub`). A rejected admin attempt (bad token, valid login but not in the admin
group, no credential at all) is itself audited as `auth.failure`.

See [configuration.md](configuration.md#admin-plane-sso-optional-off-by-default) for the
full variable list, and `src/secllm/auth.py` for the implementation.

## Audit log

Every admin-plane mutation (`model.load`, `model.unload`, `model.reload`, `model.download`
started/completed/failed) and every rejected admin attempt (`auth.failure`) is written to a
tamper-evident, hash-chained JSONL log — see [control-validation.md](control-validation.md)
for the full CMMC/NIST 800-171 control mapping, `GET /admin/api/audit` /
`/admin/api/audit/verify` to read and verify it, and `SECLLM_AUDIT_ENABLED` /
`SECLLM_AUDIT_PATH` to configure it. Records are metadata only — model ids, byte counts,
error strings, config keys — never prompt/response/weights content or a token value.

The inference path (`/v1/*`) is deliberately **not** audited here; that's SecRouter's job by
design (see "Inference governance" below).

## Inference governance is SecRouter's job

SecLLM's admin plane is the only thing gated/audited at the per-request level in this
service. Per-user identity, policy, model/tier allow-lists, egress control, per-request
audit, and budgets on the **inference path** are deliberately left to **SecRouter**, which
sits in front of SecLLM as the suite's reference audit implementation. Don't expose
`/v1` directly to end users — put SecLLM behind SecRouter (or an equivalent gateway) and let
it own that governance. See [deploy.md](deploy.md#behind-secrouter) and
[control-validation.md](control-validation.md) for the full shared-responsibility split.

## FIPS / cryptography posture

SecLLM does not bundle or select its own cryptographic implementation — it relies on the
host's OpenSSL and Python's standard crypto stack (used by `pyjwt[crypto]` for RS256/JWKS
verification and HS256 session signing). FIPS-validated cryptography is a host/OS property;
SecDeploy's `fedora-fips` native target is the reference posture for this (see
[deploy.md](deploy.md)). Network isolation, at-rest volume encryption for the data
directory, NTP, and log retention/SIEM forwarding are likewise deployment-boundary
responsibilities — see control-validation.md's "Shared Responsibility" section.

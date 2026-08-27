# SecLLM

SecLLM is a friendly, self-hosted control plane for [vLLM](https://github.com/vllm-project/vllm):
a curated model catalog, one-click load/unload/reload, GPU-aware multi-model scheduling,
automatic health management, and an admin console — speaking the OpenAI API so [SecRouter](https://github.com/secrouter/secrouter)
(or any OpenAI client) can point at it as a local, in-boundary inference provider. Part of the
[SecRouter suite](https://github.com/secrouter/secdeploy#the-suite).

- [Deploying SecLLM](deploy.md) — Docker / native / systemd, GPU scheduling, production checklist
- [Configuration](configuration.md) — every environment variable, grouped by concern
- [Usage](usage.md) — the OpenAI-compatible API, the admin API, and a console tour
- [Security](security.md) — the two auth surfaces, admin-plane SSO, audit log, FIPS posture
- [Control Validation](control-validation.md) — CMMC / NIST 800-171 control mapping for the admin plane

See the [README](../README.md) for a quickstart and the model catalog.

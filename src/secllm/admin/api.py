"""Control API (model management + health) and the console route.

Model-management endpoints are token-gated; ``/health`` and the console shell are open.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth
from ..context import Context
from ..downloads import is_cached
from ..supervisor import CapacityError, Worker
from .ui import CONSOLE_HTML


def _source_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _worker_view(w: Worker) -> dict[str, Any]:
    return {
        "state": w.state,
        "last_health": w.last_health,
        "uptime_s": round(w.uptime_s, 1),
        "port": w.port,
        "restarts": w.restarts,
        "error": w.error,
        "context_length": w.context_length,  # 0 = catalog default, no override active
        "gpus": w.gpus,  # device indices this worker is pinned to ([] = unmanaged host)
        "memory_fraction": w.memory_fraction,  # per-GPU VRAM fraction it reserves (0 = unmanaged)
    }


async def _context_length_from_body(request: Request) -> int:
    """Optional ``{"context_length": N}`` JSON body on a load/reload POST — 0 (default, also
    what an absent/empty body yields) means "no override, use the catalog's own default" (see
    Supervisor.load). A GET-style empty body is the common case (console Load/Reload button
    pressed with no override typed in), so a missing or unparsable body is never an error here."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — no body at all is the common, valid case
        return 0
    if not isinstance(body, dict):
        return 0
    value = body.get("context_length")
    return int(value) if value else 0


def build_router(ctx: Context) -> APIRouter:
    router = APIRouter()

    def require_admin(request: Request) -> str:
        """Gate an admin route, returning the caller's audit ``principal``: the OIDC ``sub`` for
        a SecSSO admin, or the literal ``"token-admin"`` for the static break-glass credential.
        Raises 401 (after recording an ``auth.failure`` audit event) if neither applies."""
        # SecSSO admin login (resolved by the auth middleware, when SSO is on) — the primary path:
        # a valid principal that is a member of the admin group (see auth.py).
        principal_obj = auth.current_principal(request) if auth.auth_enabled else None
        if principal_obj is not None and auth.is_admin(principal_obj):
            return principal_obj.sub
        # Static admin token — always accepted as bootstrap / break-glass, and the ONLY credential
        # when SSO is off. An OIDC bearer that isn't this token simply won't match here.
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if token and secrets.compare_digest(token, ctx.config.admin_token):
            return "token-admin"
        ctx.audit.record(
            "auth.failure",
            principal=principal_obj.sub if principal_obj else "anonymous",
            source_ip=_source_ip(request),
            target=None,
            outcome="deny",
            detail={
                "path": request.url.path,
                "reason": "authenticated but not in admin group" if principal_obj else "no valid credential",
            },
        )
        raise HTTPException(status_code=401, detail="SecSSO admin login or admin token required")

    @router.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse(url="/admin")

    @router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def console() -> HTMLResponse:
        return HTMLResponse(CONSOLE_HTML)

    @router.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "service": "secllm",
            "version": "1.0.0",
            "backend": ctx.config.backend,
            "loaded": [{"id": w.model_id, "state": w.state} for w in ctx.supervisor.list()],
            "auth": auth.status(),  # admin-plane SSO posture (off by default)
        })

    @router.get("/admin/api/models")
    async def list_models(request: Request) -> JSONResponse:
        require_admin(request)
        models = []
        for model in ctx.catalog.models.values():
            worker = ctx.supervisor.get(model.id)
            repo_id = model.repo_id(ctx.config.backend)
            # Live download view: status + error (as before) plus a progress % computed from
            # the cache blobs on disk vs the repo's known total (see downloads.status_view).
            download = ctx.downloads.status_view(model.id, repo_id)
            entry = {
                **model.to_dict(), "loaded": worker is not None,
                # Local-cache-only check (no network) — cheap enough for every poll. Not
                # meaningful for the mock backend (nothing real ever downloads there), but
                # harmless — it just always reads as not cached.
                "cached": is_cached(repo_id),
                "download_status": download["status"], "download_error": download["error"],
                "download_downloaded_bytes": download["downloaded_bytes"],
                "download_total_bytes": download["total_bytes"],
                "download_percent": download["percent"],
                # Live transfer rate + time-remaining while a download is in flight (both null
                # otherwise) — see downloads.status_view / Downloads._speed.
                "download_speed_bps": download["speed_bps"],
                "download_eta_seconds": download["eta_seconds"],
                # API-call tracking: this model's request/error/latency/token counters.
                "stats": ctx.stats.for_model(model.id),
            }
            if worker:
                entry["worker"] = _worker_view(worker)
            models.append(entry)
        return JSONResponse({
            "backend": ctx.config.backend,
            "max_loaded": ctx.config.max_loaded,
            "gpu": ctx.supervisor.gpu_summary(),
            "models": models,
        })

    @router.get("/admin/api/stats")
    async def stats(request: Request) -> JSONResponse:
        # API-call tracking — per-model + overall request/error counts, avg latency, token totals.
        require_admin(request)
        return JSONResponse(ctx.stats.snapshot())

    @router.post("/admin/api/models/{model_id}/download")
    async def download(request: Request, model_id: str) -> JSONResponse:
        principal = require_admin(request)
        model = ctx.catalog.get(model_id)
        if not model:
            raise HTTPException(status_code=404, detail=f"unknown model {model_id!r}")
        repo_id = model.repo_id(ctx.config.backend)

        def _on_done(mid: str, ok: bool, error: str) -> None:
            # Runs on Downloads' background thread, well after this request has returned — no
            # Request object survives that long, so this event carries no sourceIp (the
            # "started" event below already recorded the requester's).
            ctx.audit.record(
                "model.download", principal=principal, target=mid,
                outcome="ok" if ok else "error",
                detail={"repo_id": repo_id, "phase": "completed" if ok else "failed",
                        **({"error": error[:300]} if error else {})},
            )

        state = ctx.downloads.start(model_id, repo_id, on_done=_on_done)
        ctx.audit.record(
            "model.download", principal=principal, source_ip=_source_ip(request), target=model_id,
            outcome="ok", detail={"repo_id": repo_id, "phase": "started", "status": state.status},
        )
        return JSONResponse({"id": model_id, "download_status": state.status})

    @router.post("/admin/api/models/{model_id}/load")
    async def load(request: Request, model_id: str) -> JSONResponse:
        principal = require_admin(request)
        context_length = await _context_length_from_body(request)
        try:
            worker = ctx.supervisor.load(model_id, context_length=context_length)
        except KeyError as exc:
            ctx.audit.record("model.load", principal=principal, source_ip=_source_ip(request),
                              target=model_id, outcome="error", detail={"reason": "unknown model"})
            raise HTTPException(status_code=404, detail=str(exc))
        except CapacityError as exc:
            ctx.audit.record("model.load", principal=principal, source_ip=_source_ip(request),
                              target=model_id, outcome="error", detail={"reason": "no gpu capacity"})
            raise HTTPException(status_code=409, detail=str(exc))
        ctx.audit.record(
            "model.load", principal=principal, source_ip=_source_ip(request), target=model_id,
            outcome="ok", detail={"context_length": context_length, "port": worker.port},
        )
        return JSONResponse({"id": model_id, "state": worker.state, "port": worker.port,
                              "context_length": worker.context_length})

    @router.post("/admin/api/models/{model_id}/unload")
    async def unload(request: Request, model_id: str) -> JSONResponse:
        principal = require_admin(request)
        was_loaded = ctx.supervisor.get(model_id) is not None
        ctx.supervisor.unload(model_id)
        ctx.audit.record(
            "model.unload", principal=principal, source_ip=_source_ip(request), target=model_id,
            outcome="ok", detail={"was_loaded": was_loaded},
        )
        return JSONResponse({"id": model_id, "state": "stopped"})

    @router.post("/admin/api/models/{model_id}/reload")
    async def reload(request: Request, model_id: str) -> JSONResponse:
        principal = require_admin(request)
        context_length = await _context_length_from_body(request)
        try:
            worker = ctx.supervisor.reload(model_id, context_length=context_length)
        except KeyError as exc:
            ctx.audit.record("model.reload", principal=principal, source_ip=_source_ip(request),
                              target=model_id, outcome="error", detail={"reason": "unknown model"})
            raise HTTPException(status_code=404, detail=str(exc))
        except CapacityError as exc:
            ctx.audit.record("model.reload", principal=principal, source_ip=_source_ip(request),
                              target=model_id, outcome="error", detail={"reason": "no gpu capacity"})
            raise HTTPException(status_code=409, detail=str(exc))
        ctx.audit.record(
            "model.reload", principal=principal, source_ip=_source_ip(request), target=model_id,
            outcome="ok", detail={"context_length": context_length, "restarts": worker.restarts},
        )
        return JSONResponse({"id": model_id, "state": worker.state, "restarts": worker.restarts,
                              "context_length": worker.context_length})

    # ---- audit + CMMC evidence (admin-gated, same as every route above) ----------------------

    @router.get("/admin/api/audit")
    async def list_audit(request: Request) -> JSONResponse:
        """Recent admin-plane audit events, newest first. ``?type=model.load`` filters to one
        event type; ``?limit=N`` bounds the count (default 100, capped at 1000)."""
        require_admin(request)
        try:
            limit = int(request.query_params.get("limit", "100") or 100)
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 1000))
        type_filter = request.query_params.get("type") or None
        return JSONResponse({"events": ctx.audit.recent(limit=limit, type_filter=type_filter)})

    @router.get("/admin/api/audit/verify")
    async def verify_audit(request: Request) -> JSONResponse:
        """Validate the admin audit log's hash chain (AU-3.3.8) — ``{ok, checked, brokenAtSeq?}``."""
        require_admin(request)
        return JSONResponse(ctx.audit.verify())

    @router.get("/admin/api/evidence")
    async def evidence(request: Request) -> JSONResponse:
        """One-shot CMMC evidence bundle: sanitized config, audit-chain verification, the
        recent audit trail, and a control self-assessment (Spec B.6)."""
        principal = require_admin(request)
        response = JSONResponse({
            "product": "secllm",
            "version": "1.0.0",
            "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "generatedBy": principal,
            "config": _sanitized_config(ctx),
            "auditChain": ctx.audit.verify(),
            "auditRecent": ctx.audit.recent(limit=200),
            "controls": _controls_self_assessment(),
        })
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        response.headers["Content-Disposition"] = f'attachment; filename="secllm-evidence-{today}.json"'
        return response

    return router


def _sanitized_config(ctx: Context) -> dict[str, Any]:
    """Config posture for the evidence bundle — model catalog, backend, and paths only.
    NEVER the admin token, the API token, or any OIDC client secret / session secret; those
    only ever appear (redacted to a bool) as "is one configured", never the value itself."""
    cfg = ctx.config
    return {
        "backend": cfg.backend,
        "host": cfg.host,
        "port": cfg.port,
        "data_dir": str(cfg.data_dir),
        "catalog_path": cfg.catalog_path or "(built-in)",
        "max_loaded": cfg.max_loaded,
        "gpu_cap": cfg.gpu_cap,
        "gpu_devices": cfg.gpu_devices,
        "worker_host": cfg.worker_host,
        "worker_port_base": cfg.worker_port_base,
        "autostart": cfg.autostart,
        "api_token_configured": bool(cfg.api_token),
        "admin_token_generated": cfg.admin_token_generated,
        "auth": auth.status(),  # already sanitized — see auth.status()
        "models": [
            {"id": m.id, "name": m.name, "origin": m.origin, "hf_model": m.hf_model,
             "mlx_model": m.mlx_model, "size_class": m.size_class}
            for m in ctx.catalog.models.values()
        ],
    }


def _controls_self_assessment() -> list[dict[str, Any]]:
    """Self-assessment for the evidence bundle (Spec B.5 citation style: bare Family/ID)."""
    return [
        {
            "family": "AU", "id": "3.3.1",
            "requirement": "Create and retain audit records for security-relevant events",
            "implementation": "AuditLogger.record (src/secllm/audit.py), wired at every admin "
                               "mutation route in src/secllm/admin/api.py: model.load/unload/"
                               "reload/download + auth.failure",
            "evidence": "GET /admin/api/audit",
        },
        {
            "family": "AU", "id": "3.3.8",
            "requirement": "Protect audit information and audit logging tools from unauthorized "
                            "access, modification, and deletion",
            "implementation": "SHA-256 hash chain over each record's canonicalized fields "
                               "(src/secllm/audit.py verify_chain); 0600/0700 at-rest permissions",
            "evidence": "GET /admin/api/audit/verify",
        },
        {
            "family": "IA", "id": "3.5.2",
            "requirement": "Authenticate the identities of users, processes, or devices",
            "implementation": "SecSSO OIDC (bearer JWT + PKCE browser login), admin-group gate "
                               "(src/secllm/auth.py); static break-glass token as bootstrap / "
                               "air-gapped fallback, principal recorded as \"token-admin\"",
            "evidence": "GET /auth/status; config.auth in this bundle",
        },
        {
            "family": "—", "id": "delegation",
            "requirement": "Inference-path (/v1) request-level governance, per-user audit, and "
                            "usage budgets",
            "implementation": "Delegated to SecRouter BY DESIGN — SecLLM's admin plane audits "
                               "model lifecycle only and never inference requests or content",
            "evidence": "docs/deploy.md § Behind SecRouter; docs/control-validation.md",
        },
    ]

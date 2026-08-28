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
from ..catalog import Catalog, order_entry, read_catalog_file, write_catalog_file
from ..catalog import validate as validate_catalog
from ..context import Context
from ..downloads import is_cached
from ..supervisor import CapacityError, Worker
from .ui import CONSOLE_HTML


def _source_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _catalog_is_builtin(ctx: Context) -> bool:
    return not ctx.config.catalog_path


def _require_editable_catalog(ctx: Context) -> str:
    """The catalog file path, if it's actually editable — 409s (never silently materializes a
    file) when the running catalog is the built-in one, per the "read-only when built-in"
    contract: an operator must deliberately opt in with ``SECLLM_CATALOG`` before SecLLM will
    ever write a models.json on their behalf."""
    path = ctx.config.catalog_path
    if not path:
        raise HTTPException(
            status_code=409,
            detail="the built-in catalog is read-only — set SECLLM_CATALOG to a models.json "
                   "path (see models.example.json) to enable catalog edits",
        )
    return path


def _diff_entry_fields(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Field NAMES (never values — audit details are metadata-only, see the audit module
    docstring) that differ between the old and new version of one catalog entry.
    ``old=None`` (a brand-new entry) reports every field the new entry sets."""
    if old is None:
        return sorted(new)
    keys = set(old) | set(new)
    return sorted(k for k in keys if old.get(k) != new.get(k))


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
                **model.to_dict(), "loaded": worker is not None, "orphaned": False,
                # Local-cache-only check (no network) — cheap enough for every poll. Not
                # meaningful for the mock backend (nothing real ever downloads there), but
                # harmless — it just always reads as not cached. Revision-aware: a model
                # cached at a different commit than the one now pinned reads as not-cached.
                "cached": is_cached(repo_id, model.revision),
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
        # Orphaned workers: a catalog hot-reload or CRUD delete can remove an entry out from
        # under a worker that's still running (see Catalog.swap_in_place) — it keeps serving at
        # its launch-time settings, but has no catalog entry to render above, so surface it here
        # as a minimal synthetic row instead of just silently vanishing from the console/API.
        for worker in ctx.supervisor.list():
            if worker.model_id in ctx.catalog.models:
                continue
            models.append({
                "id": worker.model_id, "loaded": True, "orphaned": True,
                "worker": _worker_view(worker), "stats": ctx.stats.for_model(worker.model_id),
            })
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

        def _on_done(mid: str, ok: bool, error: str, resolved_revision: str) -> None:
            # Runs on Downloads' background thread, well after this request has returned — no
            # Request object survives that long, so this event carries no sourceIp (the
            # "started" event below already recorded the requester's). resolved_revision is the
            # EXACT commit hash the download landed at (see downloads._resolve_commit_hash) —
            # recorded so even a floating tag's audit trail pins something reproducible.
            ctx.audit.record(
                "model.download", principal=principal, target=mid,
                outcome="ok" if ok else "error",
                detail={"repo_id": repo_id, "phase": "completed" if ok else "failed",
                        **({"resolved_revision": resolved_revision} if resolved_revision else {}),
                        **({"error": error[:300]} if error else {})},
            )

        state = ctx.downloads.start(model_id, repo_id, revision=model.revision, on_done=_on_done)
        ctx.audit.record(
            "model.download", principal=principal, source_ip=_source_ip(request), target=model_id,
            outcome="ok", detail={"repo_id": repo_id, "phase": "started", "status": state.status,
                                  **({"revision": model.revision} if model.revision else {})},
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

    # ---- catalog management: hot-reload + write-through CRUD -----------------------------------

    @router.get("/admin/api/catalog")
    async def get_catalog(request: Request) -> JSONResponse:
        require_admin(request)
        return JSONResponse({
            "source": ctx.config.catalog_path or None,
            "built_in": _catalog_is_builtin(ctx),
            "count": len(ctx.catalog.models),
            "models": [m.to_dict() for m in ctx.catalog.models.values()],
        })

    @router.post("/admin/api/catalog/reload")
    async def reload_catalog(request: Request) -> JSONResponse:
        """Re-``Catalog.load`` the SAME source (built-in or ``SECLLM_CATALOG``) and atomically
        swap it in — see :meth:`Catalog.swap_in_place`. Workers already running are UNTOUCHED
        (they keep their launch-time settings regardless of what the catalog now says); only a
        future load/reload consults the new entries. A model that disappeared stays up but
        shows ``orphaned: true`` in ``GET /admin/api/models``."""
        principal = require_admin(request)
        path = ctx.config.catalog_path or None
        before = {mid: m.to_dict() for mid, m in ctx.catalog.models.items()}
        try:
            new_catalog = Catalog.load(path, backend=ctx.config.backend)
        except ValueError as exc:
            ctx.audit.record(
                "catalog.reload", principal=principal, source_ip=_source_ip(request), target=None,
                outcome="error", detail={"reason": str(exc)[:1000]},
            )
            raise HTTPException(status_code=400, detail=str(exc))

        after_ids = set(new_catalog.models)
        before_ids = set(before)
        added = sorted(after_ids - before_ids)
        removed = sorted(before_ids - after_ids)
        changed = sorted(
            mid for mid in (before_ids & after_ids)
            if before[mid] != new_catalog.models[mid].to_dict()
        )
        ctx.catalog.swap_in_place(new_catalog)
        ctx.audit.record(
            "catalog.reload", principal=principal, source_ip=_source_ip(request), target=None,
            outcome="ok", detail={"added": added, "removed": removed, "changed": changed},
        )
        return JSONResponse({"added": added, "removed": removed, "changed": changed,
                              "count": len(new_catalog.models)})

    @router.put("/admin/api/catalog/models/{model_id}")
    async def upsert_catalog_model(request: Request, model_id: str) -> JSONResponse:
        """Upsert one catalog entry: validate → write ``SECLLM_CATALOG`` atomically (preserving
        every OTHER entry's formatting) → hot-swap the in-memory catalog on the exact same path
        as ``/admin/api/catalog/reload``. 409 (never a silent file materialization) when the
        running catalog is the built-in one — see :func:`_require_editable_catalog`."""
        principal = require_admin(request)
        path = _require_editable_catalog(ctx)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — malformed/absent JSON body
            raise HTTPException(status_code=400, detail="request body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        body_id = body.get("id", model_id)
        if body_id != model_id:
            raise HTTPException(
                status_code=400,
                detail=f"body id {body_id!r} does not match path id {model_id!r}",
            )
        body = {**body, "id": model_id}

        raw = read_catalog_file(path)
        entries = raw.setdefault("models", [])
        existing_index = next((i for i, e in enumerate(entries) if e.get("id") == model_id), None)
        old_entry = entries[existing_index] if existing_index is not None else None

        trial = list(entries)
        if existing_index is not None:
            trial[existing_index] = body
        else:
            trial.append(body)
        errors = validate_catalog({**raw, "models": trial}, backend=ctx.config.backend)
        if errors:
            ctx.audit.record(
                "catalog.changed", principal=principal, source_ip=_source_ip(request),
                target=model_id, outcome="error",
                detail={"action": "upsert", "reason": "validation failed", "errors": errors[:10]},
            )
            raise HTTPException(status_code=400, detail={"errors": errors})

        new_entry = order_entry(body)
        if existing_index is not None:
            entries[existing_index] = new_entry
        else:
            entries.append(new_entry)
        write_catalog_file(path, raw)

        new_catalog = Catalog.load(path, backend=ctx.config.backend)
        ctx.catalog.swap_in_place(new_catalog)

        diff = _diff_entry_fields(old_entry, new_entry)
        ctx.audit.record(
            "catalog.changed", principal=principal, source_ip=_source_ip(request), target=model_id,
            outcome="ok", detail={"action": "upsert", "diff": diff},
        )
        return JSONResponse({
            "id": model_id, "model": ctx.catalog.get(model_id).to_dict(), "diff": diff,
        })

    @router.delete("/admin/api/catalog/models/{model_id}")
    async def delete_catalog_model(request: Request, model_id: str) -> JSONResponse:
        """Delete one catalog entry: write-through + hot-swap, same as the upsert route. A
        worker currently running this model is left alone (see ``list_models``'s ``orphaned``
        flag) — deleting the catalog entry is not the same action as unloading the worker."""
        principal = require_admin(request)
        path = _require_editable_catalog(ctx)
        raw = read_catalog_file(path)
        entries = raw.get("models", [])
        index = next((i for i, e in enumerate(entries) if e.get("id") == model_id), None)
        if index is None:
            raise HTTPException(status_code=404, detail=f"unknown model {model_id!r} in catalog")
        removed_entry = entries.pop(index)
        write_catalog_file(path, raw)

        new_catalog = Catalog.load(path, backend=ctx.config.backend)
        ctx.catalog.swap_in_place(new_catalog)

        diff = sorted(removed_entry)  # field NAMES only — never dump the entry's values
        ctx.audit.record(
            "catalog.changed", principal=principal, source_ip=_source_ip(request), target=model_id,
            outcome="ok", detail={"action": "delete", "diff": diff},
        )
        return JSONResponse({"id": model_id, "deleted": True})

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
    pinned = sum(1 for m in ctx.catalog.models.values() if m.revision)
    return {
        "backend": cfg.backend,
        "host": cfg.host,
        "port": cfg.port,
        "data_dir": str(cfg.data_dir),
        "catalog_path": cfg.catalog_path or "(built-in)",
        # Catalog change-control posture (CM) — source, size, and how much of it is pinned to
        # an exact HF commit vs floating to a repo's default branch (see Model.revision).
        "catalog_source": cfg.catalog_path or "(built-in)",
        "catalog_built_in": _catalog_is_builtin(ctx),
        "catalog_entry_count": len(ctx.catalog.models),
        "catalog_pinned_count": pinned,
        "catalog_floating_count": len(ctx.catalog.models) - pinned,
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
             "mlx_model": m.mlx_model, "size_class": m.size_class, "revision": m.revision}
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
            "family": "CM", "id": "3.4.3",
            "requirement": "Track, review, approve/disapprove, and log changes to "
                            "organizational systems",
            "implementation": "Catalog CRUD (PUT/DELETE /admin/api/catalog/models/{id}) is "
                               "admin-gated, schema-validated (src/secllm/catalog.py "
                               "validate()), write-through to the SECLLM_CATALOG file "
                               "(atomic tmp+rename), hot-swapped in memory, and audited with a "
                               "field-name-only diff (catalog.changed); model weights "
                               "themselves can be pinned to an exact HF commit "
                               "(Model.revision) — the model-weights analogue of the suite's "
                               "suite.toml dependency pinning",
            "evidence": "GET /admin/api/catalog; GET /admin/api/audit?type=catalog.changed; "
                        "GET /admin/api/audit?type=catalog.reload",
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

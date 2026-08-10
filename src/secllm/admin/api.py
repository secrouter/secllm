"""Control API (model management + health) and the console route.

Model-management endpoints are token-gated; ``/health`` and the console shell are open.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..context import Context
from ..downloads import is_cached
from ..supervisor import CapacityError, Worker
from .ui import CONSOLE_HTML


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

    def require_admin(request: Request) -> None:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if not token or not secrets.compare_digest(token, ctx.config.admin_token):
            raise HTTPException(status_code=401, detail="admin token required")

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
        require_admin(request)
        model = ctx.catalog.get(model_id)
        if not model:
            raise HTTPException(status_code=404, detail=f"unknown model {model_id!r}")
        state = ctx.downloads.start(model_id, model.repo_id(ctx.config.backend))
        return JSONResponse({"id": model_id, "download_status": state.status})

    @router.post("/admin/api/models/{model_id}/load")
    async def load(request: Request, model_id: str) -> JSONResponse:
        require_admin(request)
        context_length = await _context_length_from_body(request)
        try:
            worker = ctx.supervisor.load(model_id, context_length=context_length)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except CapacityError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return JSONResponse({"id": model_id, "state": worker.state, "port": worker.port,
                              "context_length": worker.context_length})

    @router.post("/admin/api/models/{model_id}/unload")
    async def unload(request: Request, model_id: str) -> JSONResponse:
        require_admin(request)
        ctx.supervisor.unload(model_id)
        return JSONResponse({"id": model_id, "state": "stopped"})

    @router.post("/admin/api/models/{model_id}/reload")
    async def reload(request: Request, model_id: str) -> JSONResponse:
        require_admin(request)
        context_length = await _context_length_from_body(request)
        try:
            worker = ctx.supervisor.reload(model_id, context_length=context_length)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except CapacityError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return JSONResponse({"id": model_id, "state": worker.state, "restarts": worker.restarts,
                              "context_length": worker.context_length})

    return router

"""Control API (model management + health) and the console route.

Model-management endpoints are token-gated; ``/health`` and the console shell are open.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..context import Context
from ..supervisor import Worker
from .ui import CONSOLE_HTML


def _worker_view(w: Worker) -> dict[str, Any]:
    return {
        "state": w.state,
        "last_health": w.last_health,
        "uptime_s": round(w.uptime_s, 1),
        "port": w.port,
        "restarts": w.restarts,
        "error": w.error,
    }


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
            entry = {**model.to_dict(), "loaded": worker is not None}
            if worker:
                entry["worker"] = _worker_view(worker)
            models.append(entry)
        return JSONResponse({
            "backend": ctx.config.backend,
            "max_loaded": ctx.config.max_loaded,
            "models": models,
        })

    @router.post("/admin/api/models/{model_id}/load")
    async def load(request: Request, model_id: str) -> JSONResponse:
        require_admin(request)
        try:
            worker = ctx.supervisor.load(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return JSONResponse({"id": model_id, "state": worker.state, "port": worker.port})

    @router.post("/admin/api/models/{model_id}/unload")
    async def unload(request: Request, model_id: str) -> JSONResponse:
        require_admin(request)
        ctx.supervisor.unload(model_id)
        return JSONResponse({"id": model_id, "state": "stopped"})

    @router.post("/admin/api/models/{model_id}/reload")
    async def reload(request: Request, model_id: str) -> JSONResponse:
        require_admin(request)
        try:
            worker = ctx.supervisor.reload(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return JSONResponse({"id": model_id, "state": worker.state, "restarts": worker.restarts})

    return router

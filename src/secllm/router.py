"""OpenAI-compatible router — proxies ``/v1/*`` to the loaded worker for the requested model.

This is the endpoint SecRouter points at as a local, self-hosted provider. Requests name a
model by its catalog id (``fast``, ``balanced``, …); if that model isn't loaded and healthy,
the caller gets a clear ``404``/``503`` instead of a hang.
"""

from __future__ import annotations

import json
import secrets
import time

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .context import Context


def _error(status: int, message: str, err_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": err_type}}, status_code=status)


def _usage(content: bytes) -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) from an OpenAI response body — best-effort → (0, 0)."""
    try:
        u = json.loads(content).get("usage") or {}
        return int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0)
    except (ValueError, AttributeError, TypeError):
        return 0, 0


def build_router(ctx: Context) -> APIRouter:
    router = APIRouter()

    def require_api_token(request: Request) -> JSONResponse | None:
        """Enforce ``Authorization: Bearer <SECLLM_API_TOKEN>`` on /v1/* routes.

        No-op when ``config.api_token`` is unset — the historical, open behavior (SecLLM
        relying on network isolation / sitting behind SecRouter). Returns SecLLM's
        OpenAI-style error JSON on failure instead of raising, so callers can use the
        same envelope as every other /v1 error in this router.
        """
        if not ctx.config.api_token:
            return None
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if not token or not secrets.compare_digest(token, ctx.config.api_token):
            return _error(401, "invalid or missing API token", "invalid_api_key")
        return None

    @router.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:
        if (err := require_api_token(request)) is not None:
            return err
        data = [
            {"id": w.model_id, "object": "model", "created": int(w.started_at), "owned_by": "secllm"}
            for w in ctx.supervisor.list()
            if w.state == "healthy"
        ]
        return JSONResponse({"object": "list", "data": data})

    async def _proxy(request: Request, path: str) -> Response:
        if (err := require_api_token(request)) is not None:
            return err
        t0 = time.monotonic()
        body = await request.body()
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return _error(400, "request body is not valid JSON")
        model_id = payload.get("model")
        if not model_id:
            return _error(400, "missing 'model'")

        def track(status: int, ptoks: int = 0, ctoks: int = 0) -> None:
            ctx.stats.record(model_id, path, status, (time.monotonic() - t0) * 1000.0, ptoks, ctoks)

        worker = ctx.supervisor.get(model_id)
        if not worker:
            if not ctx.catalog.get(model_id):
                track(404)
                return _error(404, f"unknown model {model_id!r}", "model_not_found")
            track(404)
            return _error(404, f"model {model_id!r} is not loaded — load it first", "model_not_loaded")
        if worker.state != "healthy":
            track(503)
            return _error(503, f"model {model_id!r} is {worker.state}", "model_unavailable")

        url = worker.base_url + path
        headers = {"Content-Type": "application/json"}
        if payload.get("stream"):
            async def stream():
                status = 200
                try:
                    async with httpx.AsyncClient(timeout=None) as client:
                        async with client.stream("POST", url, content=body, headers=headers) as resp:
                            status = resp.status_code
                            async for chunk in resp.aiter_raw():
                                yield chunk
                finally:
                    # Recorded when the stream finishes; token usage isn't available mid-stream.
                    track(status)
            return StreamingResponse(stream(), media_type="text/event-stream")

        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(url, content=body, headers=headers)
        pt, ct = _usage(resp.content)
        track(resp.status_code, pt, ct)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _proxy(request, "/v1/chat/completions")

    @router.post("/v1/completions")
    async def completions(request: Request) -> Response:
        return await _proxy(request, "/v1/completions")

    @router.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        return await _proxy(request, "/v1/embeddings")

    return router

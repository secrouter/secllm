"""OpenAI-compatible router — proxies ``/v1/*`` to the loaded worker for the requested model.

This is the endpoint SecRouter points at as a local, self-hosted provider. Requests name a
model by its catalog id (``gemma-4-26B-A4B-it``, ``gpt-oss-20b``, …); if that model isn't loaded and healthy,
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


def _stream_usage(tail: bytes) -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) from the TAIL of an SSE stream — when a client sends
    ``stream_options: {"include_usage": true}`` (OpenAI SDKs do), vLLM emits the request's
    whole-stream usage in one final ``data: {...}`` chunk just before ``data: [DONE]``.
    Scanning only a bounded tail (see ``stream()``) keeps memory flat while still catching
    that last chunk. Takes the LAST line carrying ``"usage"`` (delta chunks may echo a null
    usage field) and best-effort → (0, 0) — the historical no-usage-for-streams behavior —
    when absent or malformed (e.g. a line truncated at the tail's front edge)."""
    pt = ct = 0
    for line in tail.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:") or b'"usage"' not in line:
            continue
        try:
            u = json.loads(line[5:]).get("usage") or {}
            pt = int(u.get("prompt_tokens", 0) or 0)
            ct = int(u.get("completion_tokens", 0) or 0)
        except (ValueError, AttributeError, TypeError):
            continue
    return pt, ct


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

    def _served_context(model_id: str, worker_context: int) -> int | None:
        """The context window this worker is actually serving, for the ``max_model_len``
        field below: the per-load override when one was given, else the same catalog/
        config default chain the launcher used (see ``backends.build_launch_command``).
        ``None`` when it can't be determined — the field is then omitted rather than
        guessed, since clients size their token budgets off it."""
        if worker_context:
            return worker_context
        from .backends import _catalog_max_model_len

        model = ctx.catalog.get(model_id)
        catalog_len = _catalog_max_model_len(model) if model else None
        if catalog_len:
            return catalog_len
        if ctx.config.backend == "metal":
            return ctx.config.metal_max_model_len
        return None

    @router.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:
        if (err := require_api_token(request)) is not None:
            return err
        # ``max_model_len`` mirrors vLLM's own /v1/models extension: clients (e.g. the
        # secagent pi-guard preflight) read it to clamp their declared contextWindow to
        # what the server will actually accept, instead of dying at the cap mid-session.
        # The bare proxy payload used to strip it — that blinded the preflight entirely.
        data = []
        for w in ctx.supervisor.list():
            if w.state != "healthy":
                continue
            entry: dict[str, object] = {
                "id": w.model_id, "object": "model",
                "created": int(w.started_at), "owned_by": "secllm",
            }
            served = _served_context(w.model_id, w.context_length)
            if served:
                entry["max_model_len"] = served
            data.append(entry)
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
                # Rolling tail of the raw bytes (bounded, so a long stream never buffers whole):
                # vLLM's final usage chunk arrives just before [DONE], and concatenating chunks
                # here reassembles it even when a network boundary splits the line. 64 KiB is
                # orders of magnitude more than that last chunk + [DONE] need.
                tail = b""
                try:
                    async with httpx.AsyncClient(timeout=None) as client:
                        async with client.stream("POST", url, content=body, headers=headers) as resp:
                            status = resp.status_code
                            async for chunk in resp.aiter_raw():
                                tail = (tail + chunk)[-65536:]
                                yield chunk
                finally:
                    # Recorded when the stream finishes; per-stream usage only exists if the
                    # client asked for it (stream_options.include_usage) — else (0, 0) as before.
                    ptoks, ctoks = _stream_usage(tail)
                    track(status, ptoks, ctoks)
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

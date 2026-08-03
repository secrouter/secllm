"""End-to-end control-plane flow on the mock backend: load → serve → reload → unload."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import ASGITransport

ADMIN = {"Authorization": "Bearer test-token"}


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _wait_healthy(ctx, model_id: str, timeout: float = 15.0):
    for _ in range(int(timeout / 0.3)):
        await ctx.health.check_once()
        worker = ctx.supervisor.get(model_id)
        if worker and worker.state == "healthy":
            return worker
        await asyncio.sleep(0.3)
    raise AssertionError(f"{model_id} never became healthy")


async def test_load_serve_reload_unload(stack):
    app, ctx = stack
    ctx.supervisor.load("fast")
    worker = await _wait_healthy(ctx, "fast")
    assert worker.state == "healthy"

    async with _client(app) as c:
        r = await c.get("/v1/models")
        assert r.status_code == 200
        assert any(m["id"] == "fast" for m in r.json()["data"])

        r = await c.post("/v1/chat/completions",
                         json={"model": "fast", "messages": [{"role": "user", "content": "hello"}]})
        assert r.status_code == 200, r.text
        assert "hello" in r.json()["choices"][0]["message"]["content"]

        r = await c.post("/v1/chat/completions", json={"model": "nope", "messages": []})
        assert r.status_code == 404 and r.json()["error"]["type"] == "model_not_found"

        r = await c.post("/v1/chat/completions", json={"model": "balanced", "messages": []})
        assert r.status_code == 404 and r.json()["error"]["type"] == "model_not_loaded"

    ctx.supervisor.reload("fast")
    worker = await _wait_healthy(ctx, "fast")
    assert worker.restarts >= 1

    ctx.supervisor.unload("fast")
    async with _client(app) as c:
        r = await c.post("/v1/chat/completions", json={"model": "fast", "messages": []})
        assert r.status_code == 404


async def test_streaming_proxy(stack):
    app, ctx = stack
    ctx.supervisor.load("fast")
    await _wait_healthy(ctx, "fast")
    body = ""
    async with _client(app) as c:
        async with c.stream("POST", "/v1/chat/completions",
                            json={"model": "fast", "stream": True,
                                  "messages": [{"role": "user", "content": "hi"}]}) as r:
            assert r.status_code == 200
            async for chunk in r.aiter_text():
                body += chunk
    assert "data:" in body and "[DONE]" in body


async def test_single_gpu_switch_evicts_oldest(stack):
    # max_loaded defaults to 1 → loading a second model switches to it
    app, ctx = stack
    ctx.supervisor.load("fast")
    await _wait_healthy(ctx, "fast")
    ctx.supervisor.load("balanced")
    await _wait_healthy(ctx, "balanced")
    assert ctx.supervisor.get("fast") is None
    assert ctx.supervisor.get("balanced").state == "healthy"


async def test_admin_gating(stack):
    app, ctx = stack
    async with _client(app) as c:
        assert (await c.get("/admin/api/models")).status_code == 401
        r = await c.get("/admin/api/models", headers=ADMIN)
        assert r.status_code == 200
        assert any(m["id"] == "fast" for m in r.json()["models"])

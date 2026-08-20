"""SECLLM_API_TOKEN gating on the OpenAI-compatible /v1/* routes.

/v1 is open by default (relies on network isolation / sitting behind SecRouter). Setting
SECLLM_API_TOKEN turns on bearer-token auth for defense in depth; /health stays open
regardless, since it's used for liveness/monitoring/SecRouter breaker probes.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import ASGITransport

API_TOKEN = "secret-api-token"
API = {"Authorization": f"Bearer {API_TOKEN}"}
WRONG = {"Authorization": "Bearer wrong-token"}
CHAT_BODY = {"model": "Llama-3.2-3B-Instruct", "messages": [{"role": "user", "content": "hi"}]}


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


@pytest.mark.parametrize("stack", [API_TOKEN], indirect=True)
async def test_v1_models_requires_token_when_configured(stack):
    app, ctx = stack
    async with _client(app) as c:
        r = await c.get("/v1/models")
        assert r.status_code == 401
        assert r.json() == {"error": {"message": "invalid or missing API token", "type": "invalid_api_key"}}

        r = await c.get("/v1/models", headers=WRONG)
        assert r.status_code == 401

        r = await c.get("/v1/models", headers=API)
        assert r.status_code == 200


@pytest.mark.parametrize("stack", [API_TOKEN], indirect=True)
async def test_v1_chat_completions_requires_token_when_configured(stack):
    app, ctx = stack
    ctx.supervisor.load("Llama-3.2-3B-Instruct")
    await _wait_healthy(ctx, "Llama-3.2-3B-Instruct")

    async with _client(app) as c:
        r = await c.post("/v1/chat/completions", json=CHAT_BODY)
        assert r.status_code == 401
        assert r.json()["error"]["type"] == "invalid_api_key"

        r = await c.post("/v1/chat/completions", headers=WRONG, json=CHAT_BODY)
        assert r.status_code == 401

        r = await c.post("/v1/chat/completions", headers=API, json=CHAT_BODY)
        assert r.status_code == 200, r.text
        assert "hi" in r.json()["choices"][0]["message"]["content"]


@pytest.mark.parametrize("stack", [API_TOKEN], indirect=True)
async def test_v1_completions_and_embeddings_require_token_when_configured(stack):
    app, ctx = stack
    ctx.supervisor.load("Llama-3.2-3B-Instruct")
    await _wait_healthy(ctx, "Llama-3.2-3B-Instruct")

    async with _client(app) as c:
        r = await c.post("/v1/completions", json={"model": "Llama-3.2-3B-Instruct", "prompt": "hi"})
        assert r.status_code == 401

        r = await c.post("/v1/embeddings", json={"model": "Llama-3.2-3B-Instruct", "input": "hi"})
        assert r.status_code == 401


@pytest.mark.parametrize("stack", [API_TOKEN], indirect=True)
async def test_health_stays_open_when_token_configured(stack):
    app, ctx = stack
    async with _client(app) as c:
        r = await c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


async def test_v1_open_when_token_unset(stack):
    """Default behavior (SECLLM_API_TOKEN unset) is unchanged: /v1 stays open."""
    app, ctx = stack
    ctx.supervisor.load("Llama-3.2-3B-Instruct")
    await _wait_healthy(ctx, "Llama-3.2-3B-Instruct")

    async with _client(app) as c:
        r = await c.get("/v1/models")
        assert r.status_code == 200

        r = await c.post("/v1/chat/completions", json=CHAT_BODY)
        assert r.status_code == 200, r.text

        r = await c.get("/health")
        assert r.status_code == 200

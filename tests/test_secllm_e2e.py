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
    ctx.supervisor.load("Llama-3.2-3B-Instruct")
    worker = await _wait_healthy(ctx, "Llama-3.2-3B-Instruct")
    assert worker.state == "healthy"

    async with _client(app) as c:
        r = await c.get("/v1/models")
        assert r.status_code == 200
        assert any(m["id"] == "Llama-3.2-3B-Instruct" for m in r.json()["data"])

        r = await c.post("/v1/chat/completions",
                         json={"model": "Llama-3.2-3B-Instruct", "messages": [{"role": "user", "content": "hello"}]})
        assert r.status_code == 200, r.text
        assert "hello" in r.json()["choices"][0]["message"]["content"]

        r = await c.post("/v1/chat/completions", json={"model": "nope", "messages": []})
        assert r.status_code == 404 and r.json()["error"]["type"] == "model_not_found"

        r = await c.post("/v1/chat/completions", json={"model": "gemma-4-26B-A4B-it", "messages": []})
        assert r.status_code == 404 and r.json()["error"]["type"] == "model_not_loaded"

    ctx.supervisor.reload("Llama-3.2-3B-Instruct")
    worker = await _wait_healthy(ctx, "Llama-3.2-3B-Instruct")
    assert worker.restarts >= 1

    ctx.supervisor.unload("Llama-3.2-3B-Instruct")
    async with _client(app) as c:
        r = await c.post("/v1/chat/completions", json={"model": "Llama-3.2-3B-Instruct", "messages": []})
        assert r.status_code == 404


async def test_streaming_proxy(stack):
    app, ctx = stack
    ctx.supervisor.load("Llama-3.2-3B-Instruct")
    await _wait_healthy(ctx, "Llama-3.2-3B-Instruct")
    body = ""
    async with _client(app) as c:
        async with c.stream("POST", "/v1/chat/completions",
                            json={"model": "Llama-3.2-3B-Instruct", "stream": True,
                                  "messages": [{"role": "user", "content": "hi"}]}) as r:
            assert r.status_code == 200
            async for chunk in r.aiter_text():
                body += chunk
    assert "data:" in body and "[DONE]" in body


async def test_default_models_coexist(stack):
    # max_loaded now defaults to 0 (GPU-bound), so loading a second model NO LONGER evicts the
    # first — they coexist. On the mock backend there's no GPU inventory to bound them, so both
    # simply run at once.
    app, ctx = stack
    ctx.supervisor.load("Llama-3.2-3B-Instruct")
    await _wait_healthy(ctx, "Llama-3.2-3B-Instruct")
    ctx.supervisor.load("gemma-4-26B-A4B-it")
    await _wait_healthy(ctx, "gemma-4-26B-A4B-it")
    assert ctx.supervisor.get("Llama-3.2-3B-Instruct").state == "healthy"
    assert ctx.supervisor.get("gemma-4-26B-A4B-it").state == "healthy"


def test_max_loaded_one_restores_switch_semantics(monkeypatch, tmp_path):
    # SECLLM_MAX_LOADED=1 brings back the old hard ceiling: loading a second model evicts the
    # oldest (a "switch"). Eviction is synchronous in load(), so no health wait is needed.
    monkeypatch.setenv("SECLLM_BACKEND", "mock")
    monkeypatch.setenv("SECLLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECLLM_MAX_LOADED", "1")
    monkeypatch.setenv("SECLLM_WORKER_PORT_BASE", "12800")
    from secllm.catalog import Catalog
    from secllm.config import Config
    from secllm.supervisor import Supervisor

    cfg = Config.from_env()
    assert cfg.max_loaded == 1
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    sup = Supervisor(cfg, Catalog.load())
    try:
        sup.load("Llama-3.2-3B-Instruct")
        sup.load("gemma-4-26B-A4B-it")
        assert sup.get("Llama-3.2-3B-Instruct") is None  # evicted
        assert sup.get("gemma-4-26B-A4B-it") is not None
    finally:
        sup.stop_all()


def test_gpu_placement_spreads_across_gpus_and_rejects_when_full(monkeypatch, tmp_path):
    # Integration through the real load() path with a FAKE 2-GPU inventory injected onto the
    # supervisor (the mock backend detects no GPUs on its own). Six 0.45-fraction models under a
    # 0.95 cap: two per card fit (0.45+0.45=0.90) → 4 place across the two GPUs, the 5th 409s.
    monkeypatch.setenv("SECLLM_BACKEND", "mock")
    monkeypatch.setenv("SECLLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECLLM_WORKER_PORT_BASE", "12900")
    from secllm.catalog import Catalog, Model
    from secllm.config import Config
    from secllm.gpu import Gpu
    from secllm.supervisor import CapacityError, Supervisor

    cfg = Config.from_env()  # max_loaded=0 (GPU-bound), gpu_cap=0.95
    assert cfg.max_loaded == 0 and cfg.gpu_cap == 0.95
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(models={
        f"m{i}": Model(id=f"m{i}", name=f"m{i}", description="", hf_model=f"x/m{i}",
                       origin="US (test)", vram_fraction=0.45)
        for i in range(6)
    })
    sup = Supervisor(cfg, catalog)
    sup._gpus = [Gpu(0, "L4", 24000, 24000), Gpu(1, "L4", 24000, 24000)]
    sup._gpu_indices = [0, 1]
    try:
        w0 = sup.load("m0")
        w1 = sup.load("m1")
        # Spread: the first two workers land on DIFFERENT cards (least-loaded-first).
        assert w0.gpus and w1.gpus and w0.gpus != w1.gpus
        assert set(w0.gpus) | set(w1.gpus) == {0, 1}
        assert w0.memory_fraction == 0.45

        summary = sup.gpu_summary()
        assert summary["managed"] is True and summary["cap"] == 0.95
        assert {c["index"] for c in summary["gpus"]} == {0, 1}

        sup.load("m2")  # gpu0 or gpu1 → 0.90
        sup.load("m3")  # the other card → 0.90; both cards now full for another 0.45
        assert sum(c["allocated"] for c in sup.gpu_summary()["gpus"]) == 1.80

        import pytest as _pytest
        with _pytest.raises(CapacityError):
            sup.load("m4")  # no card can take a 5th 0.45 under the 0.95 cap → 409
        assert sup.get("m4") is None  # nothing left half-registered after the rejection
    finally:
        sup.stop_all()


async def test_admin_gating(stack):
    app, ctx = stack
    async with _client(app) as c:
        assert (await c.get("/admin/api/models")).status_code == 401
        r = await c.get("/admin/api/models", headers=ADMIN)
        assert r.status_code == 200
        assert any(m["id"] == "Llama-3.2-3B-Instruct" for m in r.json()["models"])


async def test_admin_api_models_reports_cache_and_download_status(stack, monkeypatch):
    app, ctx = stack
    monkeypatch.setattr("secllm.admin.api.is_cached", lambda repo_id: repo_id == "cached/repo")
    monkeypatch.setattr(ctx.catalog.models["Llama-3.2-3B-Instruct"], "hf_model", "cached/repo")
    async with _client(app) as c:
        r = await c.get("/admin/api/models", headers=ADMIN)
        row = next(m for m in r.json()["models"] if m["id"] == "Llama-3.2-3B-Instruct")
        assert row["cached"] is True
        assert row["download_status"] == "idle"

        other = next(m for m in r.json()["models"] if m["id"] == "gemma-4-26B-A4B-it")
        assert other["cached"] is False


async def test_admin_api_download_starts_and_reports_progress(stack, monkeypatch):
    app, ctx = stack
    events = []

    def fake_snapshot_download(repo_id, **kwargs):
        events.append(repo_id)
        return "/fake/path"

    monkeypatch.setattr("secllm.downloads.snapshot_download", fake_snapshot_download)
    async with _client(app) as c:
        r = await c.post("/admin/api/models/Llama-3.2-3B-Instruct/download", headers=ADMIN)
        assert r.status_code == 200
        assert r.json()["download_status"] in ("downloading", "complete")

        for _ in range(50):
            if ctx.downloads.status("Llama-3.2-3B-Instruct").status != "downloading":
                break
            await asyncio.sleep(0.02)
        assert ctx.downloads.status("Llama-3.2-3B-Instruct").status == "complete"
        assert events  # snapshot_download was genuinely invoked

        r = await c.post("/admin/api/models/nope-not-real/download", headers=ADMIN)
        assert r.status_code == 404


async def test_load_with_context_override_via_admin_api(stack):
    # The mock backend ignores context_length for its own launch command (see
    # build_launch_command) — this exercises the API/supervisor plumbing that's shared with
    # the real vllm/mlx backends: Worker.context_length round-trips through load, is visible
    # in /admin/api/models, and reload() without an explicit value carries it forward.
    app, ctx = stack
    async with _client(app) as c:
        r = await c.post("/admin/api/models/Llama-3.2-3B-Instruct/load", headers=ADMIN,
                         json={"context_length": 4096})
        assert r.status_code == 200 and r.json()["context_length"] == 4096
    await _wait_healthy(ctx, "Llama-3.2-3B-Instruct")

    async with _client(app) as c:
        r = await c.get("/admin/api/models", headers=ADMIN)
        row = next(m for m in r.json()["models"] if m["id"] == "Llama-3.2-3B-Instruct")
        assert row["worker"]["context_length"] == 4096

        # Reload with no body carries the prior override forward, not resetting to default.
        r = await c.post("/admin/api/models/Llama-3.2-3B-Instruct/reload", headers=ADMIN, json={})
        assert r.status_code == 200 and r.json()["context_length"] == 4096

        # An explicit new value on reload replaces it.
        r = await c.post("/admin/api/models/Llama-3.2-3B-Instruct/reload", headers=ADMIN,
                         json={"context_length": 2048})
        assert r.status_code == 200 and r.json()["context_length"] == 2048


async def test_stats_tracks_api_calls(stack):
    app, ctx = stack
    ctx.supervisor.load("Llama-3.2-3B-Instruct")
    await _wait_healthy(ctx, "Llama-3.2-3B-Instruct")

    async with _client(app) as c:
        # A served non-stream call — recorded as a success with token usage from the body.
        r = await c.post("/v1/chat/completions",
                         json={"model": "Llama-3.2-3B-Instruct", "messages": [{"role": "user", "content": "hi there"}]})
        assert r.status_code == 200

        # A streaming call — recorded when the stream finishes (no token usage mid-stream).
        async with c.stream("POST", "/v1/chat/completions",
                            json={"model": "Llama-3.2-3B-Instruct", "stream": True,
                                  "messages": [{"role": "user", "content": "go"}]}) as s:
            async for _ in s.aiter_bytes():
                pass

        # A real-but-not-loaded model — recorded against that model as an error.
        r = await c.post("/v1/chat/completions", json={"model": "gemma-4-26B-A4B-it", "messages": []})
        assert r.status_code == 404

    served = ctx.stats.for_model("Llama-3.2-3B-Instruct")
    assert served["requests"] == 2 and served["errors"] == 0 and served["last_status"] == 200
    assert served["tokens"] > 0 and served["avg_latency_ms"] is not None
    unloaded = ctx.stats.for_model("gemma-4-26B-A4B-it")
    assert unloaded["requests"] == 1 and unloaded["errors"] == 1 and unloaded["last_status"] == 404

    async with _client(app) as c:
        # Per-model stats ride along on the models list (what the console polls) …
        r = await c.get("/admin/api/models", headers=ADMIN)
        row = next(m for m in r.json()["models"] if m["id"] == "Llama-3.2-3B-Instruct")
        assert row["stats"]["requests"] == 2 and row["stats"]["tokens"] > 0

        # … and the dedicated snapshot carries per-model rows + overall totals.
        r = await c.get("/admin/api/stats", headers=ADMIN)
        assert r.status_code == 200
        snap = r.json()
        assert snap["overall"]["requests"] == 3 and snap["overall"]["errors"] == 1
        assert snap["by_model"]["Llama-3.2-3B-Instruct"]["requests"] == 2

    # The snapshot endpoint is admin-gated like the rest of the control API.
    async with _client(app) as c:
        assert (await c.get("/admin/api/stats")).status_code == 401

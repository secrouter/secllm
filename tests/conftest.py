"""Test fixtures — a SecLLM stack on the mock backend (real subprocesses, no GPU)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI


@pytest.fixture
def stack(request, tmp_path, monkeypatch):
    monkeypatch.setenv("SECLLM_BACKEND", "mock")
    monkeypatch.setenv("SECLLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECLLM_ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("SECLLM_HEALTH_TIMEOUT", "2")
    monkeypatch.setenv("SECLLM_STARTUP_GRACE", "30")
    monkeypatch.setenv("SECLLM_WORKER_PORT_BASE", "12700")
    # Optional: tests may set SECLLM_API_TOKEN via indirect parametrization, e.g.
    #   @pytest.mark.parametrize("stack", ["some-token"], indirect=True)
    # Left unset (the default) /v1 stays open, matching every test written before this existed.
    api_token = getattr(request, "param", "")
    if api_token:
        monkeypatch.setenv("SECLLM_API_TOKEN", api_token)

    from secllm.admin.api import build_router as admin_router
    from secllm.catalog import Catalog
    from secllm.config import Config
    from secllm.context import Context
    from secllm.downloads import Downloads
    from secllm.health import HealthMonitor
    from secllm.router import build_router as openai_router
    from secllm.supervisor import Supervisor

    cfg = Config.from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog.load()
    supervisor = Supervisor(cfg, catalog)
    health = HealthMonitor(cfg, supervisor)
    ctx = Context(config=cfg, catalog=catalog, supervisor=supervisor, health=health,
                  downloads=Downloads())

    app = FastAPI()
    app.include_router(openai_router(ctx))
    app.include_router(admin_router(ctx))
    try:
        yield app, ctx
    finally:
        supervisor.stop_all()

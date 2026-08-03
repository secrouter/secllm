"""FastAPI application factory — wires the catalog, supervisor, health monitor, and routers."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .admin.api import build_router as build_admin_router
from .catalog import Catalog
from .config import Config
from .context import Context
from .health import HealthMonitor
from .router import build_router as build_openai_router
from .supervisor import Supervisor

log = logging.getLogger("secllm")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog.load(config.catalog_path or None)
    supervisor = Supervisor(config, catalog)
    health = HealthMonitor(config, supervisor)
    ctx = Context(config=config, catalog=catalog, supervisor=supervisor, health=health)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        for model_id in config.autostart:
            try:
                supervisor.load(model_id)
                log.info("autostart: loading %s", model_id)
            except KeyError:
                log.warning("autostart: unknown model %r — skipping", model_id)
        health.start()
        if config.admin_token_generated:
            log.warning(
                "SECLLM_ADMIN_TOKEN was not set; generated one for this session: %s",
                config.admin_token,
            )
        log.info(
            "SecLLM ready — backend=%s port=%s catalog=%d models",
            config.backend, config.port, len(catalog.models),
        )
        yield
        await health.stop()
        supervisor.stop_all()

    app = FastAPI(
        title="SecLLM", version="1.0.0",
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
    )
    app.state.ctx = ctx
    app.include_router(build_openai_router(ctx))
    app.include_router(build_admin_router(ctx))
    return app

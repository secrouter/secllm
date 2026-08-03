"""SecLLM configuration — environment-first, with sensible single-GPU defaults."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

BACKENDS = {"vllm", "mock"}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    admin_token: str
    admin_token_generated: bool
    data_dir: Path
    catalog_path: str  # path to a models.json, or "" for the built-in catalog
    backend: str  # "vllm" | "mock"
    worker_host: str
    worker_port_base: int
    max_loaded: int  # concurrent loaded models (a single GPU is usually 1)
    autostart: list[str]  # model ids to load at boot
    health_interval: float
    health_timeout: float
    startup_grace: float  # seconds to let a worker become healthy before giving up
    gpu_memory_utilization: float
    vllm_extra_args: list[str]

    @staticmethod
    def from_env() -> "Config":
        token = _env("SECLLM_ADMIN_TOKEN", "").strip()
        generated = not token
        if generated:
            token = secrets.token_urlsafe(24)

        backend = _env("SECLLM_BACKEND", "vllm").strip().lower()
        if backend not in BACKENDS:
            raise ValueError(f"SECLLM_BACKEND must be one of {sorted(BACKENDS)}, got {backend!r}")

        autostart = [s.strip() for s in _env("SECLLM_AUTOSTART", "").split(",") if s.strip()]
        return Config(
            host=_env("SECLLM_HOST", "0.0.0.0"),
            port=_int("SECLLM_PORT", 11400),
            admin_token=token,
            admin_token_generated=generated,
            data_dir=Path(_env("SECLLM_DATA_DIR", "./data")).expanduser(),
            catalog_path=_env("SECLLM_CATALOG", ""),
            backend=backend,
            worker_host=_env("SECLLM_WORKER_HOST", "127.0.0.1"),
            worker_port_base=_int("SECLLM_WORKER_PORT_BASE", 12000),
            max_loaded=_int("SECLLM_MAX_LOADED", 1),
            autostart=autostart,
            health_interval=_float("SECLLM_HEALTH_INTERVAL", 10.0),
            health_timeout=_float("SECLLM_HEALTH_TIMEOUT", 5.0),
            startup_grace=_float("SECLLM_STARTUP_GRACE", 600.0),
            gpu_memory_utilization=_float("SECLLM_GPU_MEMORY_UTILIZATION", 0.90),
            vllm_extra_args=[s for s in _env("SECLLM_VLLM_ARGS", "").split() if s],
        )

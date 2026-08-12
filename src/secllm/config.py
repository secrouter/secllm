"""SecLLM configuration — environment-first, with sensible single-GPU defaults."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BACKENDS = {"vllm", "mock", "mlx", "metal"}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _int_list(name: str) -> list[int]:
    """A comma-separated list of GPU indices from ``name`` (e.g. ``"0,1,2"``), ignoring blanks
    and non-integers. Empty/unset → ``[]``, which the supervisor reads as "auto-detect all
    GPUs" rather than "no GPUs"."""
    result: list[int] = []
    for part in os.environ.get(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    admin_token: str
    admin_token_generated: bool
    api_token: str  # bearer token required on /v1/* when non-empty; "" = open (default)
    data_dir: Path
    catalog_path: str  # path to a models.json, or "" for the built-in catalog
    backend: str  # "vllm" | "mock" | "mlx" | "metal"
    worker_host: str
    worker_port_base: int
    # Fixed cap on concurrently loaded models. 0 (the default) = no fixed cap: models coexist,
    # bounded instead by real GPU capacity (see gpu_cap + the scheduler in gpu.py/supervisor.py).
    # >0 restores the old evict-oldest "switch" behaviour (e.g. 1 = loading a model replaces the
    # current one) for hosts that want a hard ceiling regardless of free VRAM.
    max_loaded: int
    autostart: list[str]  # model ids to load at boot
    health_interval: float
    health_timeout: float
    startup_grace: float  # seconds to let a worker become healthy before giving up
    gpu_memory_utilization: float
    vllm_extra_args: list[str]
    # Max SUM of per-model VRAM fractions the scheduler will pack onto one GPU (0.95 = fill a
    # card to 95%). Guards against co-residing two models that would together OOM it.
    gpu_cap: float = 0.95
    # Explicit GPU indices the scheduler may use (SECLLM_GPUS="0,1,2"). [] = auto-detect every
    # GPU nvidia-smi reports (see gpu.detect_gpus); ignored entirely on a mock/CPU host.
    gpu_devices: list[int] = field(default_factory=list)
    # vLLM-Metal backend (SECLLM_BACKEND=metal): the external Apple-Silicon venv (native arm64
    # Python 3.12) with vLLM + the vllm-metal plugin installed — it can't share secllm's
    # interpreter, so build_launch_command runs "<metal_venv>/bin/vllm serve" from it. Loads the
    # same MLX-format quants as the mlx backend (see catalog.Model.repo_id). Only used when
    # backend == "metal".
    metal_venv: str = "~/.venv-vllm-metal"
    metal_max_model_len: int = 8192  # default --max-model-len for metal workers (bounds KV cache)
    # Fraction of unified memory EACH metal worker may reserve (vLLM --gpu-memory-utilization).
    # vLLM pre-allocates its KV cache to this fraction at load, so the default 0.9 makes a SINGLE
    # worker grab ~90% of RAM — two co-resident models then OOM/swap the machine. 0.4 lets ~2
    # models coexist on unified memory (SECLLM_MAX_LOADED=0); lower it further for 3+.
    metal_mem_util: float = 0.4

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
            api_token=_env("SECLLM_API_TOKEN", "").strip(),
            data_dir=Path(_env("SECLLM_DATA_DIR", "./data")).expanduser(),
            catalog_path=_env("SECLLM_CATALOG", ""),
            backend=backend,
            worker_host=_env("SECLLM_WORKER_HOST", "127.0.0.1"),
            worker_port_base=_int("SECLLM_WORKER_PORT_BASE", 12000),
            max_loaded=_int("SECLLM_MAX_LOADED", 0),
            autostart=autostart,
            health_interval=_float("SECLLM_HEALTH_INTERVAL", 10.0),
            health_timeout=_float("SECLLM_HEALTH_TIMEOUT", 5.0),
            startup_grace=_float("SECLLM_STARTUP_GRACE", 600.0),
            gpu_memory_utilization=_float("SECLLM_GPU_MEMORY_UTILIZATION", 0.90),
            vllm_extra_args=[s for s in _env("SECLLM_VLLM_ARGS", "").split() if s],
            gpu_cap=_float("SECLLM_GPU_CAP", 0.95),
            gpu_devices=_int_list("SECLLM_GPUS"),
            metal_venv=_env("SECLLM_METAL_VENV", "~/.venv-vllm-metal"),
            metal_max_model_len=_int("SECLLM_METAL_MAX_MODEL_LEN", 8192),
            metal_mem_util=_float("SECLLM_METAL_MEM_UTIL", 0.4),
        )

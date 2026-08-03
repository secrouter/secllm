"""Backends — how SecLLM launches a worker (a subprocess serving OpenAI on a port).

Both backends produce an OpenAI-compatible server on ``worker_host:port`` with a ``/health``
endpoint, so the supervisor, health monitor, and router treat them identically:

* ``vllm``  — ``vllm serve <hf_model> …`` (GPU/Linux; the real inference engine).
* ``mock``  — a tiny stdlib server (:mod:`secllm.backends.mock_server`) for GPU-free dev/test.
"""

from __future__ import annotations

import sys

from ..catalog import Model
from ..config import Config

HEALTH_PATH = "/health"


def worker_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def build_launch_command(cfg: Config, model: Model, port: int) -> list[str]:
    if cfg.backend == "mock":
        return [
            sys.executable, "-m", "secllm.backends.mock_server",
            "--host", cfg.worker_host, "--port", str(port), "--model", model.id,
        ]
    # vLLM: expose the friendly catalog id as the served model name.
    cmd = [
        "vllm", "serve", model.hf_model,
        "--host", cfg.worker_host,
        "--port", str(port),
        "--served-model-name", model.id,
        "--gpu-memory-utilization", str(cfg.gpu_memory_utilization),
    ]
    return cmd + list(model.vllm_args) + list(cfg.vllm_extra_args)

"""Backends — how SecLLM launches a worker (a subprocess serving OpenAI on a port).

Every backend produces an OpenAI-compatible server on ``worker_host:port`` with a ``/health``
endpoint, so the supervisor, health monitor, and router treat them identically:

* ``vllm``  — ``vllm serve <hf_model> …`` (GPU/Linux; the real inference engine).
* ``mlx``   — a tiny stdlib server (:mod:`secllm.backends.mlx_server`) using Apple's ``mlx-lm``;
  the real inference engine on Apple Silicon (macOS-only optional dependency, see pyproject.toml).
* ``mock``  — a tiny stdlib server (:mod:`secllm.backends.mock_server`) for GPU-free dev/test.
"""

from __future__ import annotations

import sys

from ..catalog import Model
from ..config import Config

HEALTH_PATH = "/health"


def worker_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _vllm_args_with_context(model_args: list[str], context_length: int | None) -> list[str]:
    """``model_args`` (the catalog's own ``vllm_args``) with ``--max-model-len`` overridden to
    ``context_length`` when given — replacing an existing ``--max-model-len`` pair if the
    catalog entry already sets one, appending it otherwise. ``None`` (the default — no admin
    override for this load) returns ``model_args`` untouched."""
    if context_length is None:
        return list(model_args)
    args = list(model_args)
    if "--max-model-len" in args:
        args[args.index("--max-model-len") + 1] = str(context_length)
        return args
    return args + ["--max-model-len", str(context_length)]


def build_launch_command(
    cfg: Config, model: Model, port: int, context_length: int | None = None,
) -> list[str]:
    """``context_length`` (tokens), when given, overrides the catalog's own context-length
    default for THIS load only — vLLM's ``--max-model-len`` or the mlx backend's own
    ``--max-context`` (see :mod:`secllm.backends.mlx_server`). ``None`` (the default) leaves
    each backend at its catalog-configured default, unchanged from before this override
    existed. The mock backend ignores it entirely — it does no real inference, so a context
    cap is meaningless there."""
    if cfg.backend == "mock":
        return [
            sys.executable, "-m", "secllm.backends.mock_server",
            "--host", cfg.worker_host, "--port", str(port), "--model", model.id,
        ]
    if cfg.backend == "mlx":
        cmd = [
            sys.executable, "-m", "secllm.backends.mlx_server",
            "--host", cfg.worker_host, "--port", str(port), "--model", model.id,
            "--hf-model", model.repo_id("mlx"),
        ]
        if context_length is not None:
            cmd += ["--max-context", str(context_length)]
        return cmd
    # vLLM: expose the friendly catalog id as the served model name.
    cmd = [
        "vllm", "serve", model.hf_model,
        "--host", cfg.worker_host,
        "--port", str(port),
        "--served-model-name", model.id,
        "--gpu-memory-utilization", str(cfg.gpu_memory_utilization),
    ]
    return cmd + _vllm_args_with_context(model.vllm_args, context_length) + list(cfg.vllm_extra_args)

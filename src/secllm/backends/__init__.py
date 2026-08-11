"""Backends — how SecLLM launches a worker (a subprocess serving OpenAI on a port).

Every backend produces an OpenAI-compatible server on ``worker_host:port`` with a ``/health``
endpoint, so the supervisor, health monitor, and router treat them identically:

* ``vllm``  — ``vllm serve <hf_model> …`` (GPU/Linux; the real inference engine).
* ``mlx``   — a tiny stdlib server (:mod:`secllm.backends.mlx_server`) using Apple's ``mlx-lm``;
  the real inference engine on Apple Silicon (macOS-only optional dependency, see pyproject.toml).
* ``metal`` — vLLM's OWN OpenAI server (``vllm serve``) run from a separate Apple-Silicon venv
  with the vllm-metal plugin (``SECLLM_METAL_VENV``). Full vLLM engine on Metal — supports model
  architectures mlx-lm lacks (e.g. Gemma-4 unified). Loads the same MLX-format quants as ``mlx``.
* ``mock``  — a tiny stdlib server (:mod:`secllm.backends.mock_server`) for GPU-free dev/test.
"""

from __future__ import annotations

import sys
from pathlib import Path

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
    memory_fraction: float | None = None,
) -> list[str]:
    """``context_length`` (tokens), when given, overrides the catalog's own context-length
    default for THIS load only — vLLM's ``--max-model-len`` or the mlx backend's own
    ``--max-context`` (see :mod:`secllm.backends.mlx_server`). ``None`` (the default) leaves
    each backend at its catalog-configured default, unchanged from before this override
    existed. The mock backend ignores it entirely — it does no real inference, so a context
    cap is meaningless there.

    ``memory_fraction`` (0..1), when given, is the per-GPU VRAM share the co-residency
    scheduler (see :mod:`secllm.gpu`) picked for this worker — passed to vLLM as
    ``--gpu-memory-utilization`` so two models on one card don't each grab the global default
    and OOM it. ``None`` (unmanaged host / no GPU inventory) keeps ``cfg.gpu_memory_utilization``,
    exactly as before. Only the vLLM path uses it; mlx and mock ignore it."""
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
    if cfg.backend == "metal":
        # vLLM's own OpenAI server, run from the external vllm-metal venv (separate 3.12
        # interpreter — can't be imported here). Loads the same MLX quant as the mlx backend
        # (repo_id("metal")); --served-model-name exposes the friendly catalog id. --enforce-eager
        # skips torch.compile/CUDAGraph setup (no Triton on Metal — vLLM falls back anyway).
        vllm_bin = str(Path(cfg.metal_venv).expanduser() / "bin" / "vllm")
        return [
            vllm_bin, "serve", model.repo_id("metal"),
            "--host", cfg.worker_host,
            "--port", str(port),
            "--served-model-name", model.id,
            "--max-model-len", str(context_length or cfg.metal_max_model_len),
            "--enforce-eager",
        ]
    # vLLM: expose the friendly catalog id as the served model name.
    cmd = [
        "vllm", "serve", model.hf_model,
        "--host", cfg.worker_host,
        "--port", str(port),
        "--served-model-name", model.id,
        "--gpu-memory-utilization", str(memory_fraction or cfg.gpu_memory_utilization),
    ]
    return cmd + _vllm_args_with_context(model.vllm_args, context_length) + list(cfg.vllm_extra_args)

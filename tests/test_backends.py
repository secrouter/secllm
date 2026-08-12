"""Unit tests for build_launch_command's context-length override wiring across backends —
pure command construction, no subprocess involved (that's covered by test_secllm_e2e.py's
real-mock-subprocess flow)."""

from __future__ import annotations

from dataclasses import replace

from secllm.backends import _vllm_args_with_context, build_launch_command
from secllm.catalog import Model
from secllm.config import Config


def _cfg(backend: str) -> Config:
    return Config(
        host="0.0.0.0", port=11400, admin_token="t", admin_token_generated=False,
        api_token="", data_dir="/tmp/x", catalog_path="", backend=backend,
        worker_host="127.0.0.1", worker_port_base=12000, max_loaded=1, autostart=[],
        health_interval=10.0, health_timeout=5.0, startup_grace=600.0,
        gpu_memory_utilization=0.9, vllm_extra_args=[],
    )


def _model(**overrides) -> Model:
    base = dict(
        id="reasoning", name="Reasoning", description="d", hf_model="openai/gpt-oss-20b",
        origin="US (OpenAI)", vllm_args=["--max-model-len", "32768"],
        mlx_model="mlx-community/gpt-oss-20b-MXFP4-Q8",
    )
    base.update(overrides)
    return Model(**base)


def test_vllm_args_with_context_replaces_existing_max_model_len():
    args = _vllm_args_with_context(["--max-model-len", "32768", "--other", "x"], 8192)
    assert args == ["--max-model-len", "8192", "--other", "x"]


def test_vllm_args_with_context_appends_when_absent():
    args = _vllm_args_with_context(["--other", "x"], 8192)
    assert args == ["--other", "x", "--max-model-len", "8192"]


def test_vllm_args_with_context_none_leaves_untouched():
    original = ["--max-model-len", "32768"]
    assert _vllm_args_with_context(original, None) == original


def test_build_launch_command_vllm_no_override_uses_catalog_default():
    cmd = build_launch_command(_cfg("vllm"), _model(), port=12000)
    assert cmd[:2] == ["vllm", "serve"]
    assert "--max-model-len" in cmd and cmd[cmd.index("--max-model-len") + 1] == "32768"


def test_build_launch_command_vllm_override_replaces_catalog_default():
    cmd = build_launch_command(_cfg("vllm"), _model(), port=12000, context_length=4096)
    assert cmd[cmd.index("--max-model-len") + 1] == "4096"
    assert cmd.count("--max-model-len") == 1  # replaced, not duplicated


def test_build_launch_command_mlx_no_override_omits_max_context():
    cmd = build_launch_command(_cfg("mlx"), _model(), port=12000)
    assert "--max-context" not in cmd


def test_build_launch_command_mlx_override_adds_max_context():
    cmd = build_launch_command(_cfg("mlx"), _model(), port=12000, context_length=16384)
    assert cmd[cmd.index("--max-context") + 1] == "16384"


def test_build_launch_command_mock_ignores_context_length():
    cmd = build_launch_command(_cfg("mock"), _model(), port=12000, context_length=16384)
    assert "--max-context" not in cmd and "--max-model-len" not in cmd


def test_build_launch_command_vllm_uses_cfg_utilization_by_default():
    cmd = build_launch_command(_cfg("vllm"), _model(), port=12000)
    assert cmd[cmd.index("--gpu-memory-utilization") + 1] == "0.9"


def test_build_launch_command_vllm_memory_fraction_overrides_utilization():
    # The scheduler-chosen per-GPU fraction replaces the global default for a co-resident worker.
    cmd = build_launch_command(_cfg("vllm"), _model(), port=12000, memory_fraction=0.45)
    assert cmd[cmd.index("--gpu-memory-utilization") + 1] == "0.45"


def test_build_launch_command_mlx_ignores_memory_fraction():
    cmd = build_launch_command(_cfg("mlx"), _model(), port=12000, memory_fraction=0.45)
    assert "--gpu-memory-utilization" not in cmd and "0.45" not in cmd


def test_build_launch_command_metal_serves_mlx_repo_from_external_venv():
    # metal runs `vllm serve` from the external vllm-metal venv, loading the MLX quant
    # (repo_id("metal")) and exposing the friendly catalog id as the served model name.
    cfg = replace(_cfg("metal"), metal_venv="/opt/vm")
    cmd = build_launch_command(cfg, _model(), port=12000)
    assert cmd[:2] == ["/opt/vm/bin/vllm", "serve"]
    assert cmd[2] == "mlx-community/gpt-oss-20b-MXFP4-Q8"  # the MLX quant, not hf_model
    assert cmd[cmd.index("--served-model-name") + 1] == "reasoning"
    assert cmd[cmd.index("--max-model-len") + 1] == "8192"  # cfg.metal_max_model_len default
    assert "--enforce-eager" in cmd
    assert "--gpu-memory-utilization" not in cmd  # no GPU knobs on Metal


def test_build_launch_command_metal_context_override_replaces_max_model_len():
    cfg = replace(_cfg("metal"), metal_venv="/opt/vm")
    cmd = build_launch_command(cfg, _model(), port=12000, context_length=4096)
    assert cmd[cmd.index("--max-model-len") + 1] == "4096"
    assert cmd.count("--max-model-len") == 1  # overridden, not duplicated

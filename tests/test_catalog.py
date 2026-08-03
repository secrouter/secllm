"""The built-in catalog encodes the suite's supply-chain posture."""

from __future__ import annotations

from secllm.catalog import Catalog


def test_builtin_has_the_curated_tiers():
    catalog = Catalog.load()
    assert {"fast", "balanced", "reasoning", "large"} <= set(catalog.ids())


def test_builtin_is_us_origin_only():
    catalog = Catalog.load()
    for model in catalog.models.values():
        assert model.origin.startswith("US"), f"{model.id} is not marked US-origin"


def test_builtin_excludes_prc_jurisdiction_models():
    catalog = Catalog.load()
    banned = ("qwen", "deepseek", "yi-", "internlm", "kimi", "moonshot", "glm")
    for model in catalog.models.values():
        hf = model.hf_model.lower()
        assert not any(b in hf for b in banned), f"{model.hf_model} should not be a default"

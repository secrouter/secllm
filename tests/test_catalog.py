"""The built-in catalog encodes the suite's supply-chain posture."""

from __future__ import annotations

import json

import pytest

from secllm.catalog import (
    Catalog,
    order_entry,
    read_catalog_file,
    validate,
    write_catalog_file,
)


def test_builtin_has_the_curated_models():
    catalog = Catalog.load()
    assert {
        "Llama-3.2-3B-Instruct", "gemma-4-26B-A4B-it", "gpt-oss-20b", "Llama-3.3-70B-Instruct"
    } <= set(catalog.ids())


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


def test_builtin_models_default_to_unpinned_revision():
    # revision existed before as an unwritten default — confirm it's None (float), not "" or
    # some other falsy-but-wrong sentinel, for every shipped model.
    catalog = Catalog.load()
    for model in catalog.models.values():
        assert model.revision is None


# ---- validate() — the shared boot/reload/CRUD validation matrix -------------------------------


def _entry(**overrides):
    base = {"id": "m1", "hf_model": "org/m1"}
    base.update(overrides)
    return base


def test_validate_accepts_a_minimal_valid_entry():
    assert validate({"models": [_entry()]}) == []


def test_validate_rejects_non_dict_top_level():
    assert validate([]) != []
    assert validate({"models": "nope"}) != []


def test_validate_rejects_missing_required_fields():
    errors = validate({"models": [{"name": "no id or hf_model"}]})
    assert any("id" in e for e in errors)
    assert any("hf_model" in e for e in errors)


def test_validate_rejects_duplicate_ids():
    errors = validate({"models": [_entry(id="dup"), _entry(id="dup", hf_model="org/other")]})
    assert any("duplicate" in e and "dup" in e for e in errors)


def test_validate_rejects_unknown_keys_fail_loud():
    errors = validate({"models": [_entry(bogus_field="x")]})
    assert any("unknown key" in e and "bogus_field" in e for e in errors)


def test_validate_vram_fraction_must_be_in_0_1():
    assert validate({"models": [_entry(vram_fraction=0.5)]}) == []
    assert validate({"models": [_entry(vram_fraction=0)]}) == []  # 0 = explicit "unset"
    assert any("vram_fraction" in e for e in validate({"models": [_entry(vram_fraction=1.5)]}))
    assert any("vram_fraction" in e for e in validate({"models": [_entry(vram_fraction=-0.1)]}))
    assert any("vram_fraction" in e for e in validate({"models": [_entry(vram_fraction="0.5")]}))


def test_validate_context_length_must_be_non_negative_int():
    assert validate({"models": [_entry(context_length=8192)]}) == []
    assert any("context_length" in e for e in validate({"models": [_entry(context_length=-1)]}))
    assert any("context_length" in e for e in validate({"models": [_entry(context_length=1.5)]}))


def test_validate_vllm_args_must_be_list_of_strings():
    assert validate({"models": [_entry(vllm_args=["--foo", "bar"])]}) == []
    assert any("vllm_args" in e for e in validate({"models": [_entry(vllm_args="not-a-list")]}))
    assert any("vllm_args" in e for e in validate({"models": [_entry(vllm_args=[1, 2])]}))


def test_validate_sampling_override_must_be_object():
    assert validate({"models": [_entry(sampling_override={"temperature": 0.0})]}) == []
    assert any(
        "sampling_override" in e
        for e in validate({"models": [_entry(sampling_override=["nope"])]})
    )


def test_validate_revision_must_be_string_or_null():
    assert validate({"models": [_entry(revision="abc123")]}) == []
    assert validate({"models": [_entry(revision=None)]}) == []
    assert any("revision" in e for e in validate({"models": [_entry(revision=123)]}))


def test_validate_backend_conditional_mlx_missing_is_a_warning_not_an_error(caplog):
    """A model with no mlx_model is perfectly valid on vllm — only a WARNING on mlx/metal,
    never added to the returned errors (backend-conditional soundness is soft, not fail-loud)."""
    with caplog.at_level("WARNING", logger="secllm.catalog"):
        errors = validate({"models": [_entry()]}, backend="mlx")
    assert errors == []
    assert any("mlx_model" in r.message for r in caplog.records)


def test_validate_no_warning_when_backend_not_given_or_mlx_model_present():
    assert validate({"models": [_entry()]}) == []  # no backend → no warning path taken at all
    assert validate({"models": [_entry(mlx_model="org/m1-mlx")]}, backend="mlx") == []


# ---- Catalog.load() fail-loud behavior ---------------------------------------------------------


def test_catalog_load_raises_value_error_with_every_problem_on_an_invalid_file(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": [
        {"name": "missing id and hf_model"},
        {"id": "dup", "hf_model": "org/a"},
        {"id": "dup", "hf_model": "org/b"},
    ]}))
    with pytest.raises(ValueError) as exc_info:
        Catalog.load(str(path))
    msg = str(exc_info.value)
    assert "missing required field" in msg
    assert "duplicate" in msg


def test_catalog_load_valid_file_behavior_is_unchanged(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": [
        {"id": "m1", "hf_model": "org/m1", "vram_fraction": 0.2, "revision": "deadbeef"},
    ]}))
    catalog = Catalog.load(str(path))
    m = catalog.get("m1")
    assert m.hf_model == "org/m1"
    assert m.vram_fraction == 0.2
    assert m.revision == "deadbeef"


# ---- Catalog.swap_in_place() — the atomic hot-reload mechanism ---------------------------------


def test_swap_in_place_updates_models_on_the_same_instance():
    original = Catalog.load()
    other = Catalog(models={"only": original.get("gpt-oss-20b")})
    original.swap_in_place(other)
    assert original.ids() == ["only"]
    # Same object identity — anything else holding `original` (e.g. Supervisor) sees the swap.
    assert original is not other


# ---- catalog file read/write helpers (CRUD write-through) --------------------------------------


def test_write_catalog_file_is_atomic_and_pretty(tmp_path):
    path = tmp_path / "models.json"
    write_catalog_file(str(path), {"models": [{"id": "m1", "hf_model": "org/m1"}]})
    text = path.read_text()
    assert text.endswith("\n")
    assert "  " in text  # indented, not minified
    assert not list(tmp_path.glob("*.tmp*"))  # no leftover tmp file
    assert read_catalog_file(str(path)) == {"models": [{"id": "m1", "hf_model": "org/m1"}]}


def test_order_entry_only_includes_present_keys_in_canonical_order():
    entry = order_entry({"vram_fraction": 0.2, "id": "m1", "hf_model": "org/m1"})
    assert list(entry) == ["id", "hf_model", "vram_fraction"]  # canonical order, not input order


def test_order_entry_is_total_even_for_a_hypothetical_unknown_key():
    entry = order_entry({"id": "m1", "surprise": 1})
    assert entry == {"id": "m1", "surprise": 1}

"""The model catalog — the curated, friendly-named models SecLLM can serve.

The built-in default is deliberately restricted to **US-origin open-weight models**, matching
the SecRouter suite's supply-chain posture (PRC-jurisdiction models such as Qwen/DeepSeek are
intentionally excluded from the defaults). Operators can add any model by editing a
``models.json`` and pointing ``SECLLM_CATALOG`` at it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger("secllm.catalog")

# Every field a catalog entry may set — used by validate() to reject unknown keys fail-loud
# (a typo'd field name silently doing nothing would be worse than a loud rejection). Keep this
# in sync with Model's fields below.
KNOWN_MODEL_KEYS = frozenset({
    "id", "name", "description", "hf_model", "origin", "size_class",
    "context_length", "vram_fraction", "vllm_args", "mlx_model",
    "tool_call_parser", "sampling_override", "revision",
})
REQUIRED_MODEL_KEYS = ("id", "hf_model")
# Canonical on-disk field order for a written entry (CRUD write-through) — deterministic
# regardless of the order a client's JSON body happened to use, so edits produce clean,
# reviewable git diffs instead of reordering unrelated fields.
CANONICAL_MODEL_KEY_ORDER = (
    "id", "name", "description", "hf_model", "origin", "size_class",
    "context_length", "vram_fraction", "vllm_args", "mlx_model",
    "tool_call_parser", "sampling_override", "revision",
)


@dataclass
class Model:
    id: str  # the OpenAI model name clients use (e.g. "gemma-4-26B-A4B-it")
    name: str  # friendly display name
    description: str
    hf_model: str  # the Hugging Face repo id vLLM loads
    origin: str  # provenance, e.g. "US (Meta)" — surfaced for supply-chain review
    size_class: str = "medium"  # small | medium | large
    context_length: int = 0
    # Fraction of ONE GPU this model reserves, for the co-residency scheduler (see gpu.py):
    # how much VRAM to hand vLLM (--gpu-memory-utilization) and how much of a card it consumes
    # when deciding whether another model still fits. 0 (the default) falls back to the global
    # SECLLM_GPU_MEMORY_UTILIZATION. A tensor-parallel model reserves this fraction on EACH GPU
    # it spans. Left 0 for a whole-card tensor-parallel model (TP across whole cards, so per-card
    # packing is moot).
    vram_fraction: float = 0.0
    vllm_args: list[str] = field(default_factory=list)
    # The MLX-converted repo id (e.g. "mlx-community/...-4bit") the mlx backend loads instead of
    # hf_model — MLX's fast path wants pre-quantized weights in its own format, not raw vLLM
    # safetensors (see backends/mlx_server.py). Empty (the default) falls back to hf_model, which
    # only works if that repo happens to already be MLX-format.
    mlx_model: str = ""
    # vLLM's tool-call parser for this model (its `--tool-call-parser`), enabling server-side
    # function/tool calling on the vllm + metal backends: without it vLLM rejects tool requests
    # (400 "auto tool choice requires --enable-auto-tool-choice and --tool-call-parser") and the
    # model emits tool-call JSON as plain text instead of real tool_calls. Model-specific —
    # `llama3_json` for Llama 3.x, `gemma4` for Gemma 4, etc. (see `vllm serve --tool-call-parser`
    # choices). Empty (the default) = tool calling stays off for this model.
    tool_call_parser: str = ""
    # Default sampling overrides for this model — passed to the vllm/metal backend as vLLM's
    # --override-generation-config, applied when a request omits the param. Lets a model that
    # garbles at its own default ship a saner one: the Gemma 4 26B 4-bit quant gives stray non-Latin
    # tokens inflated logits, so ANY temperature > 0 eventually samples them — {"temperature": 0.0}
    # (greedy/argmax) is the only reliably clean default. Greedy decoding, in turn, is prone to
    # repetition attractors in long agent loops (the model re-emits the IDENTICAL tool call after
    # seeing its result, indefinitely — observed live: 6+ verbatim repeats of one grep until the
    # context filled); a mild "repetition_penalty" (vLLM applies it over prompt+generated tokens)
    # breaks the attractor without the temperature>0 garbling. Keep it mild (~1.1): strong values
    # degrade legitimately-repetitive structured output (JSON keys, repeated symbol names).
    # Empty ({}) = leave the model's config alone.
    sampling_override: dict[str, Any] = field(default_factory=dict)
    # Pin this model to an exact Hugging Face commit hash (or tag) — the model-weights analogue
    # of the suite's suite.toml dependency pinning: a fixed, reproducible, supply-chain-reviewed
    # set of weights instead of "whatever main/the tag currently points at". None (the default)
    # preserves today's behavior — FLOAT to the repo's default branch, exactly as before this
    # field existed. Threaded through downloads (snapshot_download's own revision=), the
    # download cache check (is_cached), and the backend launch command (vllm/metal --revision;
    # the mlx backend's CLI equivalent) — see backends/__init__.py and downloads.py.
    revision: str | None = None

    def repo_id(self, backend: str) -> str:
        """The actual Hugging Face repo id ``backend`` loads — ``mlx_model`` (falling back to
        ``hf_model`` if unset) for the MLX-weight backends (``mlx`` and ``metal`` — vLLM-Metal
        loads the same pre-quantized MLX repos), ``hf_model`` for everything else. Single source
        of truth for this fallback — :func:`backends.build_launch_command` and the download-cache
        check (:mod:`secllm.downloads`) both need the EXACT same resolution, or a model could
        show as "cached" for one and not the other."""
        return (self.mlx_model or self.hf_model) if backend in ("mlx", "metal") else self.hf_model

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "hf_model": self.hf_model,
            "origin": self.origin,
            "size_class": self.size_class,
            "context_length": self.context_length,
            "vram_fraction": self.vram_fraction,
            "mlx_model": self.mlx_model,
            "tool_call_parser": self.tool_call_parser,
            "sampling_override": self.sampling_override,
            "revision": self.revision,
        }


@dataclass
class Catalog:
    models: dict[str, Model]

    def get(self, model_id: str) -> Model | None:
        return self.models.get(model_id)

    def ids(self) -> list[str]:
        return list(self.models)

    def swap_in_place(self, other: "Catalog") -> None:
        """Atomically adopt ``other``'s models — a single attribute reassignment (a dict
        reference swap, atomic under the GIL — no reader ever observes a half-updated
        mapping). Used by the hot-reload endpoint and catalog CRUD (``admin/api.py``) instead
        of replacing the ``Catalog`` object itself: ``Context.catalog`` and
        ``Supervisor.catalog`` are handed the SAME instance at app startup (see ``app.py`` /
        ``context.py``), so mutating this instance's ``models`` is instantly visible to the
        supervisor too, with no extra wiring. This is deliberately NOT a deep merge: workers
        already running keep their launch-time settings regardless (``Supervisor.load``/
        ``reload`` only ever consult ``self.catalog.get(id)`` at call time, never caching the
        ``Model``), so only *future* load/reload calls observe the swap — exactly the
        "loaded workers keep running, only new loads see new entries" contract."""
        self.models = other.models

    @staticmethod
    def load(path: str | None = None, *, backend: str | None = None) -> "Catalog":
        """Parse + validate a ``models.json`` (or the built-in catalog when ``path`` is
        ``None``) and build a :class:`Catalog`. Shared entry point for boot (``app.py``), the
        hot-reload endpoint, and catalog CRUD (all three call this on the exact same path so
        they can never disagree about what "valid" means).

        Fail-loud on an invalid file — raises :class:`ValueError` with every problem found (not
        just the first), same as before this validation existed (a missing required field used
        to raise a bare ``KeyError`` on the first bad entry; this is strictly more informative,
        not a behavior change on a VALID file). ``backend``, if given, only affects which
        backend-conditional WARNINGS get logged (see :func:`validate`) — it never turns a
        warning into a fail-loud error.
        """
        raw = Path(path).read_text() if path else _BUILTIN
        data = json.loads(raw)
        errors = validate(data, backend=backend)
        if errors:
            where = f" ({path})" if path else " (built-in)"
            raise ValueError(
                f"invalid model catalog{where}:\n" + "\n".join(f"  - {e}" for e in errors)
            )
        models: dict[str, Model] = {}
        for m in data.get("models", []):
            models[m["id"]] = Model(
                id=m["id"],
                name=m.get("name", m["id"]),
                description=m.get("description", ""),
                hf_model=m["hf_model"],
                origin=m.get("origin", "unspecified"),
                size_class=m.get("size_class", "medium"),
                context_length=m.get("context_length", 0),
                vram_fraction=m.get("vram_fraction", 0.0),
                vllm_args=list(m.get("vllm_args", [])),
                mlx_model=m.get("mlx_model", ""),
                tool_call_parser=m.get("tool_call_parser", ""),
                sampling_override=dict(m.get("sampling_override", {})),
                revision=m.get("revision"),
            )
        return Catalog(models=models)


def validate(data: Any, *, backend: str | None = None) -> list[str]:
    """Validate a raw catalog dict (parsed JSON of a ``models.json``) — the single source of
    truth shared by boot load (:meth:`Catalog.load`), the hot-reload endpoint, and catalog CRUD
    (``admin/api.py``), so every entry point enforces exactly the same rules.

    Returns a list of FAIL-LOUD error strings (empty == valid): top-level shape, required
    fields, duplicate ids, unknown keys (rejected, never silently dropped), and type/range
    sanity for the fields that have one (``vram_fraction`` in ``(0, 1]``, a non-negative
    ``context_length``, ``vllm_args`` as a list of strings, ``sampling_override`` as an
    object). ``vram_fraction: 0`` is exempt from the range check — it's the documented "unset,
    fall back to the global default" sentinel (see ``Model.vram_fraction``), not an error.

    Backend-conditional soundness (e.g. a model with no ``mlx_model`` — fine on ``vllm``, a
    problem on ``mlx``/``metal``) is deliberately NOT an error here: a model that only needs to
    run on one backend shouldn't be rejected for the others. When ``backend`` is given, those
    get logged as a ``logging.warning`` instead — never added to the returned list, never
    fail-loud.
    """
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return ['catalog must be a JSON object with a top-level "models" list']
    errors: list[str] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(data["models"]):
        if not isinstance(entry, dict):
            errors.append(f"models[{i}]: must be an object, got {type(entry).__name__}")
            continue
        model_id = entry.get("id")
        label = f"model {model_id!r}" if isinstance(model_id, str) and model_id else f"models[{i}]"

        unknown = sorted(set(entry) - KNOWN_MODEL_KEYS)
        if unknown:
            errors.append(f"{label}: unknown key(s) {unknown}")

        for key in REQUIRED_MODEL_KEYS:
            if not entry.get(key):
                errors.append(f"{label}: missing required field {key!r}")

        if isinstance(model_id, str) and model_id:
            if model_id in seen_ids:
                errors.append(f"duplicate model id {model_id!r}")
            seen_ids.add(model_id)

        if "vram_fraction" in entry:
            vram = entry["vram_fraction"]
            if isinstance(vram, bool) or not isinstance(vram, (int, float)):
                errors.append(f"{label}: vram_fraction must be a number, got {vram!r}")
            elif vram != 0 and not (0 < vram <= 1):
                errors.append(f"{label}: vram_fraction must be in (0, 1] (or 0 = unset), got {vram!r}")

        if "context_length" in entry:
            cl = entry["context_length"]
            if isinstance(cl, bool) or not isinstance(cl, int) or cl < 0:
                errors.append(f"{label}: context_length must be a non-negative integer, got {cl!r}")

        if "vllm_args" in entry:
            args = entry["vllm_args"]
            if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
                errors.append(f"{label}: vllm_args must be a list of strings")

        if "sampling_override" in entry and not isinstance(entry["sampling_override"], dict):
            errors.append(f"{label}: sampling_override must be an object")

        for str_key in ("name", "description", "origin", "size_class", "hf_model",
                        "mlx_model", "tool_call_parser"):
            if str_key in entry and not isinstance(entry[str_key], str):
                errors.append(f"{label}: {str_key} must be a string")

        if "revision" in entry and entry["revision"] is not None and not isinstance(entry["revision"], str):
            errors.append(f"{label}: revision must be a string or null")

        if backend in ("mlx", "metal") and not entry.get("mlx_model"):
            _log.warning(
                "catalog: model %r has no mlx_model — the %s backend will fall back to "
                "hf_model, which only works if that repo happens to already be MLX-format",
                model_id, backend,
            )

    return errors


def read_catalog_file(path: str) -> dict[str, Any]:
    """The raw parsed JSON of a ``models.json`` — used by catalog CRUD to edit ONE entry
    in-place while leaving every other entry's formatting untouched."""
    return json.loads(Path(path).read_text())


def write_catalog_file(path: str, data: dict[str, Any]) -> None:
    """Atomically (tmp file + rename, same directory — a reader never observes a
    partially-written file) write ``data`` back to ``path`` as pretty JSON (2-space indent,
    trailing newline) matching the hand-authored style of the built-in catalog /
    ``models.example.json`` — a CRUD edit produces a clean, reviewable git diff, not a
    minified one-liner."""
    target = Path(path)
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, target)


def order_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Re-key one model entry into the catalog's canonical field order — including only the
    keys ACTUALLY PRESENT in ``entry`` (never injecting a default for one that's absent), so a
    minimal upsert body produces a minimal, clean file entry rather than every field the
    ``Model`` dataclass happens to default."""
    ordered = {k: entry[k] for k in CANONICAL_MODEL_KEY_ORDER if k in entry}
    # Anything outside the canonical tuple shouldn't exist post-validate() (unknown keys are
    # rejected fail-loud) — but keep this total rather than silently lossy just in case.
    ordered.update({k: v for k, v in entry.items() if k not in ordered})
    return ordered


# Built-in default catalog — US-origin open-weight models only.
# mlx_model: the MLX-converted repo the "mlx" backend loads (Apple Silicon — see
# backends/mlx_server.py); hf_model stays the vLLM (GPU/Linux) repo either way.
_BUILTIN = r"""
{
  "_note": "US-origin open-weight models (SecRouter supply-chain posture). Edit + point SECLLM_CATALOG at your own file to change this; PRC-jurisdiction models are excluded from the defaults.",
  "models": [
    {
      "id": "Llama-3.2-3B-Instruct",
      "name": "Llama 3.2 3B Instruct",
      "description": "Small Meta model; low latency for simple tasks.",
      "hf_model": "meta-llama/Llama-3.2-3B-Instruct",
      "mlx_model": "mlx-community/Llama-3.2-3B-Instruct-4bit",
      "origin": "US (Meta)",
      "size_class": "small",
      "context_length": 16384,
      "vram_fraction": 0.15,
      "vllm_args": ["--max-model-len", "16384"],
      "tool_call_parser": "llama3_json"
    },
    {
      "id": "gemma-4-26B-A4B-it",
      "name": "Gemma 4 26B (A4B MoE)",
      "description": "Google's mixture-of-experts (4B active of 26B total); high-throughput reasoning at a fraction of the dense per-token cost. (The 12B is a unified multimodal checkpoint that doesn't serve as text, so this is the 26B.)",
      "hf_model": "google/gemma-4-26B-A4B-it",
      "mlx_model": "mlx-community/gemma-4-26b-a4b-it-4bit",
      "origin": "US (Google)",
      "size_class": "medium",
      "context_length": 262144,
      "vram_fraction": 0.35,
      "vllm_args": ["--max-model-len", "32768"],
      "tool_call_parser": "gemma4",
      "sampling_override": {"temperature": 0.0, "top_p": 0.9, "repetition_penalty": 1.1}
    },
    {
      "id": "gpt-oss-20b",
      "name": "gpt-oss-20b",
      "description": "OpenAI open-weight reasoning model; efficient. Same family SecRouter defaults to on Bedrock.",
      "hf_model": "openai/gpt-oss-20b",
      "mlx_model": "mlx-community/gpt-oss-20b-MXFP4-Q8",
      "origin": "US (OpenAI)",
      "size_class": "medium",
      "context_length": 32768,
      "vram_fraction": 0.55,
      "vllm_args": []
    },
    {
      "id": "Llama-3.3-70B-Instruct",
      "name": "Llama 3.3 70B Instruct",
      "description": "High quality; needs a large or multi-GPU host.",
      "hf_model": "meta-llama/Llama-3.3-70B-Instruct",
      "mlx_model": "mlx-community/Llama-3.3-70B-Instruct-4bit",
      "origin": "US (Meta)",
      "size_class": "large",
      "context_length": 32768,
      "vram_fraction": 0.90,
      "vllm_args": ["--tensor-parallel-size", "2"],
      "tool_call_parser": "llama3_json"
    },
    {
      "id": "gemma-4-31B-it",
      "name": "Gemma 4 — 31B",
      "description": "Google's flagship dense model; 256K context, strong reasoning/coding — bridges server-grade quality and local execution.",
      "hf_model": "google/gemma-4-31B-it",
      "mlx_model": "mlx-community/gemma-4-31b-it-4bit",
      "origin": "US (Google)",
      "size_class": "medium",
      "context_length": 262144,
      "vram_fraction": 0.45,
      "vllm_args": ["--max-model-len", "32768"],
      "tool_call_parser": "gemma4",
      "sampling_override": {"temperature": 0.0, "top_p": 0.9}
    }
  ]
}
"""

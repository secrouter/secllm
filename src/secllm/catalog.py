"""The model catalog — the curated, friendly-named models SecLLM can serve.

The built-in default is deliberately restricted to **US-origin open-weight models**, matching
the SecRouter suite's supply-chain posture (PRC-jurisdiction models such as Qwen/DeepSeek are
intentionally excluded from the defaults). Operators can add any model by editing a
``models.json`` and pointing ``SECLLM_CATALOG`` at it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
        }


@dataclass
class Catalog:
    models: dict[str, Model]

    def get(self, model_id: str) -> Model | None:
        return self.models.get(model_id)

    def ids(self) -> list[str]:
        return list(self.models)

    @staticmethod
    def load(path: str | None = None) -> "Catalog":
        raw = Path(path).read_text() if path else _BUILTIN
        data = json.loads(raw)
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
            )
        return Catalog(models=models)


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

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
    id: str  # the OpenAI model name clients use (e.g. "balanced")
    name: str  # friendly display name
    description: str
    hf_model: str  # the Hugging Face repo id vLLM loads
    origin: str  # provenance, e.g. "US (Meta)" — surfaced for supply-chain review
    size_class: str = "medium"  # small | medium | large
    context_length: int = 0
    vllm_args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "hf_model": self.hf_model,
            "origin": self.origin,
            "size_class": self.size_class,
            "context_length": self.context_length,
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
                vllm_args=list(m.get("vllm_args", [])),
            )
        return Catalog(models=models)


# Built-in default catalog — US-origin open-weight models only.
_BUILTIN = r"""
{
  "_note": "US-origin open-weight models (SecRouter supply-chain posture). Edit + point SECLLM_CATALOG at your own file to change this; PRC-jurisdiction models are excluded from the defaults.",
  "models": [
    {
      "id": "fast",
      "name": "Fast — Llama 3.2 3B",
      "description": "Small Meta model; low latency for simple tasks.",
      "hf_model": "meta-llama/Llama-3.2-3B-Instruct",
      "origin": "US (Meta)",
      "size_class": "small",
      "context_length": 16384,
      "vllm_args": ["--max-model-len", "16384"]
    },
    {
      "id": "balanced",
      "name": "Balanced — Llama 3.1 8B",
      "description": "General-purpose 8B; solid quality at moderate GPU cost.",
      "hf_model": "meta-llama/Llama-3.1-8B-Instruct",
      "origin": "US (Meta)",
      "size_class": "medium",
      "context_length": 16384,
      "vllm_args": ["--max-model-len", "16384"]
    },
    {
      "id": "reasoning",
      "name": "Reasoning — gpt-oss-20b",
      "description": "OpenAI open-weight reasoning model; efficient. Same family SecRouter defaults to on Bedrock.",
      "hf_model": "openai/gpt-oss-20b",
      "origin": "US (OpenAI)",
      "size_class": "medium",
      "context_length": 32768,
      "vllm_args": []
    },
    {
      "id": "large",
      "name": "Large — Llama 3.3 70B",
      "description": "High quality; needs a large or multi-GPU host.",
      "hf_model": "meta-llama/Llama-3.3-70B-Instruct",
      "origin": "US (Meta)",
      "size_class": "large",
      "context_length": 32768,
      "vllm_args": ["--tensor-parallel-size", "2"]
    }
  ]
}
"""

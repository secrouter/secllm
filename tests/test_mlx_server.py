"""Unit tests for the MLX backend's own context enforcement (mlx_server.py) — the OpenAI proxy
happy path (streaming/non-streaming shape, real generation) is covered by live manual testing
(see secdeploy session notes), not here. This covers what's specific to MLX: real inference was
already verified separately; mlx-lm is an Apple-Silicon-only optional dependency (see
pyproject.toml's `mlx` extra) — skipped everywhere it isn't installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mlx_lm")

from secllm.backends import mlx_server  # noqa: E402


class _StubTokenizer:
    """Stands in for mlx_lm's TokenizerWrapper: apply_chat_template renders each message as
    ONE fake token id (so token counts are simply message count), making the drop-oldest-
    message truncation exactly predictable without loading a real model."""

    def apply_chat_template(self, messages, add_generation_prompt=True):
        return list(range(len(messages) + (1 if add_generation_prompt else 0)))


def test_fit_prompt_to_context_no_cap_returns_everything():
    tok = _StubTokenizer()
    messages = [{"role": "user", "content": f"msg{i}"} for i in range(5)]
    ids, n = mlx_server._fit_prompt_to_context(tok, messages, max_context=0)
    assert n == len(ids) == 6  # 5 messages + 1 generation-prompt token, nothing dropped


def test_fit_prompt_to_context_drops_oldest_but_keeps_system():
    tok = _StubTokenizer()
    messages = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(6)
    ]
    # Untruncated: 7 messages + 1 = 8 tokens. Dropping the oldest turn one at a time:
    # system+6 turns=8 -> system+5=7 -> system+4=6 -> system+3=5 -> system+2=4 (<=4, stop).
    ids, n = mlx_server._fit_prompt_to_context(tok, messages, max_context=4)
    assert n == 4 and len(ids) == 4  # fits, and the system message survived every drop


def test_fit_prompt_to_context_single_message_returned_even_if_over_cap():
    tok = _StubTokenizer()
    messages = [{"role": "user", "content": "only one, but huge"}]
    ids, n = mlx_server._fit_prompt_to_context(tok, messages, max_context=1)
    assert n == 2  # 1 message + 1 generation-prompt token — can't drop further, returned anyway


def test_generate_clamps_max_tokens_to_remaining_context(monkeypatch):
    calls = {}

    def fake_stream_generate(model, tokenizer, ids, **kwargs):
        calls.update(kwargs)
        return iter([])

    monkeypatch.setattr(mlx_server, "stream_generate", fake_stream_generate)
    list(mlx_server._generate(
        model=None, tokenizer=None, ids=[0, 1, 2, 3, 4],
        prompt_tokens=5, req={"max_tokens": 1000}, max_context=10,
    ))
    assert calls["max_tokens"] == 5  # 10 - 5, clamped down from the requested 1000


def test_generate_leaves_max_tokens_alone_when_uncapped(monkeypatch):
    calls = {}

    def fake_stream_generate(model, tokenizer, ids, **kwargs):
        calls.update(kwargs)
        return iter([])

    monkeypatch.setattr(mlx_server, "stream_generate", fake_stream_generate)
    list(mlx_server._generate(
        model=None, tokenizer=None, ids=[0, 1, 2], prompt_tokens=3,
        req={"max_tokens": 500}, max_context=0,
    ))
    assert calls["max_tokens"] == 500

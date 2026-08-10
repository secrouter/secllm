"""Unit tests for the API-call tracker (stats.py)."""

from __future__ import annotations

from secllm.stats import Stats


def test_records_requests_latency_and_tokens():
    s = Stats()
    s.record("fast", "/v1/chat/completions", 200, 100.0, prompt_tokens=10, completion_tokens=5)
    s.record("fast", "/v1/chat/completions", 200, 200.0, prompt_tokens=20, completion_tokens=10)
    v = s.for_model("fast")
    assert v["requests"] == 2 and v["errors"] == 0
    assert v["avg_latency_ms"] == 150.0
    assert v["prompt_tokens"] == 30 and v["completion_tokens"] == 15 and v["tokens"] == 45


def test_counts_errors_by_status():
    s = Stats()
    s.record("fast", "/v1/chat/completions", 200, 10.0)
    s.record("fast", "/v1/chat/completions", 503, 5.0)
    s.record("fast", "/v1/chat/completions", 404, 5.0)
    v = s.for_model("fast")
    assert v["requests"] == 3 and v["errors"] == 2 and v["last_status"] == 404


def test_unknown_model_is_zeroed():
    v = Stats().for_model("never-called")
    assert v["requests"] == 0 and v["avg_latency_ms"] is None and v["tokens"] == 0 and v["last_request"] is None


def test_snapshot_overall_and_per_model():
    s = Stats()
    s.record("fast", "/v1/chat/completions", 200, 100.0, 10, 5)
    s.record("balanced", "/v1/chat/completions", 200, 300.0, 30, 20)
    snap = s.snapshot()
    assert set(snap["by_model"]) == {"fast", "balanced"}
    overall = snap["overall"]
    assert overall["requests"] == 2 and overall["tokens"] == 65
    assert overall["avg_latency_ms"] == 200.0  # (100 + 300) / 2
    assert isinstance(snap["since"], float)

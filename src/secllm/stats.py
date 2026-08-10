"""API call tracking — lightweight, in-memory usage counters for the ``/v1/*`` inference API.

Every proxied inference call is recorded per model: request + error counts, average latency, prompt/
completion token totals (from the OpenAI ``usage`` block when present), and the last call's time and
status. Surfaced on the admin console + ``GET /admin/api/stats`` for at-a-glance "what's actually
being used, how fast, and how much." In-memory (resets on restart) and lock-guarded so the async
router and the admin reads never race; a durable/Prometheus export is a straightforward follow-on.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class ModelStat:
    requests: int = 0
    errors: int = 0  # calls that returned a >= 400 status
    total_latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    last_request: float | None = None
    last_status: int = 0


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_model: dict[str, ModelStat] = {}
        self.started = time.time()

    def record(self, model: str, path: str, status: int, latency_ms: float,
               prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        with self._lock:
            s = self._by_model.setdefault(model, ModelStat())
            s.requests += 1
            if status >= 400:
                s.errors += 1
            s.total_latency_ms += latency_ms
            s.prompt_tokens += prompt_tokens
            s.completion_tokens += completion_tokens
            s.last_request = time.time()
            s.last_status = status

    def for_model(self, model: str) -> dict:
        """The stats view for one model (zeroed when it has never been called) — folded into each
        row of the admin models list."""
        with self._lock:
            s = self._by_model.get(model)
            return self._view(s) if s else self._view(ModelStat())

    def snapshot(self) -> dict:
        """Overall + per-model totals for ``GET /admin/api/stats``."""
        with self._lock:
            by_model = {m: self._view(s) for m, s in self._by_model.items()}
            overall = ModelStat()
            for s in self._by_model.values():
                overall.requests += s.requests
                overall.errors += s.errors
                overall.total_latency_ms += s.total_latency_ms
                overall.prompt_tokens += s.prompt_tokens
                overall.completion_tokens += s.completion_tokens
            return {"since": self.started, "overall": self._view(overall), "by_model": by_model}

    @staticmethod
    def _view(s: ModelStat) -> dict:
        avg = round(s.total_latency_ms / s.requests, 1) if s.requests else None
        return {
            "requests": s.requests,
            "errors": s.errors,
            "avg_latency_ms": avg,
            "prompt_tokens": s.prompt_tokens,
            "completion_tokens": s.completion_tokens,
            "tokens": s.prompt_tokens + s.completion_tokens,
            "last_request": s.last_request,
            "last_status": s.last_status,
        }

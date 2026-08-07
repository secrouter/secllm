"""Model weight downloads — decoupled from loading, so an operator can pre-fetch a model's
weights (a slow, one-time network cost) without also starting a worker process to serve it.

Runs in a background THREAD (not a subprocess, unlike :class:`~secllm.supervisor.Supervisor` —
this is pure I/O against the Hugging Face Hub, no GPU/Metal state to isolate) so a caller polls
:meth:`Downloads.status` rather than blocking the request that started it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from huggingface_hub import snapshot_download

# Files that actually matter for inference (weights, config, tokenizer) — README.md/
# .gitattributes/etc. are routinely missing from an otherwise-complete local cache (confirmed
# live: a model mlx_lm.load() runs perfectly fine still reports "incomplete" under a strict
# local_files_only check) and would make an already-usable model show as "not cached" forever.
_ESSENTIAL_PATTERNS = ["*.json", "*.safetensors", "*.bin", "*.model", "tokenizer*"]


def is_cached(repo_id: str) -> bool:
    """Whether ``repo_id``'s essential files are already present in the local HF cache —
    checked WITHOUT any network access (``local_files_only=True``), so this is cheap enough to
    call on every ``GET /admin/api/models``."""
    try:
        snapshot_download(repo_id, local_files_only=True, allow_patterns=_ESSENTIAL_PATTERNS)
        return True
    except Exception:  # noqa: BLE001 — any failure (not cached, incomplete, bad repo) means no
        return False


@dataclass
class DownloadState:
    status: str = "idle"  # idle | downloading | complete | error
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0


class Downloads:
    """Tracks in-flight/completed model downloads by catalog model id (not repo id — several
    catalog entries could theoretically share a repo, and callers already think in model ids)."""

    def __init__(self) -> None:
        self._states: dict[str, DownloadState] = {}
        self._lock = threading.Lock()

    def status(self, model_id: str) -> DownloadState:
        with self._lock:
            return self._states.get(model_id, DownloadState())

    def start(self, model_id: str, repo_id: str) -> DownloadState:
        """Kick off a download for ``model_id`` (backed by ``repo_id``) if one isn't already
        in flight or already complete-and-cached. Idempotent: calling this again while a
        download is running just returns the current (in-progress) state rather than starting
        a second, redundant download."""
        with self._lock:
            existing = self._states.get(model_id)
            if existing and existing.status == "downloading":
                return existing
            state = DownloadState(status="downloading", started_at=time.time())
            self._states[model_id] = state

        def _run() -> None:
            try:
                snapshot_download(repo_id)
                with self._lock:
                    state.status = "complete"
                    state.finished_at = time.time()
            except Exception as e:  # noqa: BLE001 — surfaced via status(), not raised here
                with self._lock:
                    state.status = "error"
                    state.error = str(e)
                    state.finished_at = time.time()

        threading.Thread(target=_run, daemon=True).start()
        return state

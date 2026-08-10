"""Model weight downloads — decoupled from loading, so an operator can pre-fetch a model's
weights (a slow, one-time network cost) without also starting a worker process to serve it.

Runs in a background THREAD (not a subprocess, unlike :class:`~secllm.supervisor.Supervisor` —
this is pure I/O against the Hugging Face Hub, no GPU/Metal state to isolate) so a caller polls
:meth:`Downloads.status` rather than blocking the request that started it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.constants import HF_HUB_CACHE

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


def _repo_total_bytes(repo_id: str) -> int:
    """Total on-Hub size (bytes) of ``repo_id``'s files, for a download progress denominator.
    Best-effort: any failure (offline, gated repo, transient error) returns 0, which the
    progress view reads as "total unknown" (percent ``None``) — never a download failure."""
    try:
        info = HfApi().model_info(repo_id, files_metadata=True)
        return sum(int(s.size or 0) for s in (info.siblings or []))
    except Exception:  # noqa: BLE001 — progress is decorative; never let it break a download
        return 0


def _cache_blobs_bytes(repo_id: str) -> int:
    """Bytes currently on disk for ``repo_id`` — the size of its cache *blobs* directory
    (``<HF cache>/models--<org>--<name>/blobs/``), where the Hub writes the real file content
    (the snapshots dir is just symlinks into it). Summed following symlinks, so an in-progress
    download's partial ``.incomplete`` blobs count toward the live numerator. 0 when nothing's
    been fetched yet. ``HF_HUB_CACHE`` is read at call time so a test can repoint the cache."""
    folder = "models--" + repo_id.replace("/", "--")
    blobs = Path(HF_HUB_CACHE) / folder / "blobs"
    if not blobs.is_dir():
        return 0
    total = 0
    for f in blobs.rglob("*"):
        try:
            if f.is_file():  # follows symlinks; skips dir entries
                total += f.stat().st_size
        except OSError:
            continue
    return total


@dataclass
class DownloadState:
    status: str = "idle"  # idle | downloading | complete | error
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    total_bytes: int = 0  # repo's total size on the Hub (0 = not yet known / lookup failed)


class Downloads:
    """Tracks in-flight/completed model downloads by catalog model id (not repo id — several
    catalog entries could theoretically share a repo, and callers already think in model ids)."""

    def __init__(self) -> None:
        self._states: dict[str, DownloadState] = {}
        self._lock = threading.Lock()

    def status(self, model_id: str) -> DownloadState:
        with self._lock:
            return self._states.get(model_id, DownloadState())

    def status_view(self, model_id: str, repo_id: str) -> dict:
        """The download state PLUS a live progress reading for the console. While a download is
        in flight, ``downloaded_bytes`` is measured fresh from the cache blobs on disk and
        ``percent`` is that over the repo's known total (``None`` if the total lookup hasn't
        landed / failed). A completed download reports 100%; idle/errored report no percent.
        Cheap for idle models (no disk walk) — only an in-flight one scans the blobs dir."""
        state = self.status(model_id)
        total = state.total_bytes
        if state.status == "downloading":
            downloaded = _cache_blobs_bytes(repo_id)
            percent = round(100 * downloaded / total, 1) if total else None
        elif state.status == "complete":
            downloaded = total
            percent = 100.0
        else:  # idle | error — nothing meaningful in flight
            downloaded = 0
            percent = None
        return {
            "status": state.status,
            "error": state.error,
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "percent": percent,
        }

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

        def _measure() -> None:
            # Best-effort progress denominator, on its OWN daemon thread so a slow/hanging HF
            # metadata call can never delay the download's status transitions below (and, being
            # a daemon, never blocks shutdown). 0 on failure → percent just stays unknown.
            total = _repo_total_bytes(repo_id)
            if total:
                with self._lock:
                    state.total_bytes = total

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

        threading.Thread(target=_measure, daemon=True).start()
        threading.Thread(target=_run, daemon=True).start()
        return state

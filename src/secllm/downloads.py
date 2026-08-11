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
    """Bytes currently on disk for ``repo_id`` — the content of its cache *blobs* directory
    (``<HF cache>/models--<org>--<name>/blobs/``), where the Hub writes real file content (the
    snapshots dir is just symlinks into it). 0 when nothing's been fetched yet. ``HF_HUB_CACHE``
    is read at call time so a test can repoint the cache.

    Counts each blob AT MOST ONCE. A single blob can appear on disk as the finalized file
    (``<hash>``) and/or one-or-more partial ``<hash>.<uuid>.incomplete`` files left by
    interrupted/retried attempts — naively summing them all double-counts and can push the live
    progress numerator PAST the repo's real size (the >100% bug). So collapse by blob hash: the
    finalized file if present, else the largest partial (the furthest-along attempt)."""
    folder = "models--" + repo_id.replace("/", "--")
    blobs = Path(HF_HUB_CACHE) / folder / "blobs"
    if not blobs.is_dir():
        return 0
    finalized: dict[str, int] = {}      # blob hash -> size of the completed blob
    partials: dict[str, int] = {}       # blob hash -> largest partial seen for it
    for f in blobs.iterdir():           # blobs/ is flat — no recursion needed
        try:
            if not f.is_file():
                continue
            size = f.stat().st_size
        except OSError:
            continue
        if f.name.endswith(".incomplete"):
            h = f.name.split(".", 1)[0]
            partials[h] = max(partials.get(h, 0), size)
        else:
            finalized[f.name] = size
    return sum(finalized.values()) + sum(s for h, s in partials.items() if h not in finalized)


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
        # Last (timestamp, downloaded_bytes) reading per model, taken on the previous
        # status_view poll — lets us report a LIVE transfer rate over the inter-poll interval
        # rather than only the average since start. Guarded by _lock alongside _states.
        self._samples: dict[str, tuple[float, int]] = {}
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
        speed_bps: float | None = None  # bytes/sec; only meaningful while downloading
        eta_seconds: float | None = None
        if state.status == "downloading":
            downloaded = _cache_blobs_bytes(repo_id)
            # min(100): the numerator is a live disk measure and the denominator a metadata
            # lookup — never let rounding or a slight metadata undercount render as >100%.
            percent = min(100.0, round(100 * downloaded / total, 1)) if total else None
            speed_bps = self._speed(model_id, downloaded, state.started_at)
            if speed_bps and total:
                eta_seconds = round(max(0, total - downloaded) / speed_bps, 1)
        elif state.status == "complete":
            downloaded = total
            percent = 100.0
            with self._lock:
                self._samples.pop(model_id, None)  # done — drop the rolling sample
        else:  # idle | error — nothing meaningful in flight
            downloaded = 0
            percent = None
        return {
            "status": state.status,
            "error": state.error,
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "percent": percent,
            "speed_bps": speed_bps,
            "eta_seconds": eta_seconds,
        }

    def _speed(self, model_id: str, downloaded: int, started_at: float) -> float | None:
        """Live transfer rate (bytes/sec): bytes gained since the previous status_view poll over
        the elapsed interval. Falls back to the average since ``started_at`` when there's no prior
        sample yet or the interval is too short to be meaningful (< 0.5s). ``None`` until at least
        some bytes have landed. Stores this poll's reading for the next call."""
        now = time.time()
        with self._lock:
            prev = self._samples.get(model_id)
            self._samples[model_id] = (now, downloaded)
        if prev is not None:
            dt, db = now - prev[0], downloaded - prev[1]
            if dt >= 0.5 and db >= 0:
                return db / dt
        elapsed = now - started_at if started_at else 0
        return downloaded / elapsed if elapsed > 0 and downloaded > 0 else None

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

"""Unit tests for downloads.py — the cache-check + background-download tracker behind the
admin console's Download button. snapshot_download itself is monkeypatched throughout: these
tests are about is_cached()'s filtering and Downloads' state machine, not real HF Hub I/O.
"""

from __future__ import annotations

import time

from secllm import downloads as dl


def test_is_cached_true_when_snapshot_download_succeeds(monkeypatch):
    monkeypatch.setattr(dl, "snapshot_download", lambda *a, **k: "/fake/path")
    assert dl.is_cached("some/repo") is True


def test_is_cached_false_when_snapshot_download_raises(monkeypatch):
    def _raise(*a, **k):
        raise Exception("not cached")  # noqa: TRY002 — matching is_cached's own broad except
    monkeypatch.setattr(dl, "snapshot_download", _raise)
    assert dl.is_cached("some/repo") is False


def test_is_cached_filters_to_essential_files(monkeypatch):
    """The actual bug this exists to avoid: a real, fully-usable model was reported 'not
    cached' by a strict local_files_only check because only README.md/.gitattributes were
    missing — confirmed live against mlx-community/Llama-3.2-3B-Instruct-4bit. Assert the
    allow_patterns passed through only cover files inference actually needs."""
    captured = {}

    def fake_snapshot_download(repo_id, **kwargs):
        captured.update(kwargs)
        return "/fake/path"

    monkeypatch.setattr(dl, "snapshot_download", fake_snapshot_download)
    dl.is_cached("some/repo")
    assert captured["local_files_only"] is True
    assert "*.md" not in captured["allow_patterns"]
    assert "*.safetensors" in captured["allow_patterns"]


def test_downloads_start_marks_downloading_then_complete(monkeypatch):
    # A mocked snapshot_download is effectively instant, so the background thread can flip
    # the state to "complete" before this line even runs — "downloading" is what start()
    # itself always sets synchronously (see its docstring), not something guaranteed still
    # observable a moment later; assert the eventual, settled state instead.
    monkeypatch.setattr(dl, "snapshot_download", lambda *a, **k: "/fake/path")
    d = dl.Downloads()
    d.start("fast", "some/repo")
    for _ in range(50):
        if d.status("fast").status != "downloading":
            break
        time.sleep(0.02)
    assert d.status("fast").status == "complete"
    assert d.status("fast").finished_at > 0


def test_downloads_start_marks_error_on_failure(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("network is down")
    monkeypatch.setattr(dl, "snapshot_download", _raise)
    d = dl.Downloads()
    d.start("fast", "some/repo")
    for _ in range(50):
        if d.status("fast").status != "downloading":
            break
        time.sleep(0.02)
    state = d.status("fast")
    assert state.status == "error"
    assert "network is down" in state.error


def test_downloads_start_sets_downloading_synchronously_before_returning(monkeypatch):
    """Unlike the instant-mock test above, this uses a slow mock specifically so the
    "downloading" state is reliably observable — proving start() itself sets it before
    spawning the background thread, not racing to see whichever state happens to land first."""
    monkeypatch.setattr(dl, "snapshot_download", lambda *a, **k: time.sleep(0.3) or "/fake/path")
    d = dl.Downloads()
    state = d.start("fast", "some/repo")
    assert state.status == "downloading"
    assert d.status("fast").status == "downloading"


def test_downloads_start_is_idempotent_while_in_flight(monkeypatch):
    """A second Download click while one's already running must not spawn a second thread —
    confirmed by checking start() returns the SAME state object, not a fresh one."""
    started = []

    def slow_download(*a, **k):
        started.append(1)
        time.sleep(0.3)
        return "/fake/path"

    monkeypatch.setattr(dl, "snapshot_download", slow_download)
    d = dl.Downloads()
    first = d.start("fast", "some/repo")
    second = d.start("fast", "some/repo")
    assert first is second
    time.sleep(0.5)
    assert len(started) == 1


def test_downloads_status_defaults_to_idle_for_unknown_model():
    d = dl.Downloads()
    assert d.status("never-touched").status == "idle"


# ---- download progress (status_view + total_bytes) --------------------------------------------


def test_status_view_computes_percent_from_blobs(monkeypatch, tmp_path):
    """The live numerator is the size of the repo's cache *blobs* dir; the denominator is the
    repo's known total. Point the HF cache at a tmp dir, write 250 of 1000 bytes → 25%."""
    monkeypatch.setattr(dl, "HF_HUB_CACHE", str(tmp_path))
    d = dl.Downloads()
    d._states["fast"] = dl.DownloadState(status="downloading", total_bytes=1000)
    blobs = tmp_path / "models--meta-llama--Llama-3.2-3B-Instruct" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "deadbeef").write_bytes(b"x" * 250)
    view = d.status_view("fast", "meta-llama/Llama-3.2-3B-Instruct")
    assert view["status"] == "downloading"
    assert view["downloaded_bytes"] == 250
    assert view["total_bytes"] == 1000
    assert view["percent"] == 25.0


def test_status_view_dedupes_finalized_and_incomplete_blobs(monkeypatch, tmp_path):
    """Regression (>100% bug): a blob can sit on disk as its finalized file AND leftover
    ``<hash>.<uuid>.incomplete`` partials from retried attempts, and a still-downloading blob can
    have several partials at once. Summing all of them double-counts and pushes percent past 100.
    Each blob must count once — the finalized file if present, else its largest partial."""
    monkeypatch.setattr(dl, "HF_HUB_CACHE", str(tmp_path))
    d = dl.Downloads()
    d._states["fast"] = dl.DownloadState(status="downloading", total_bytes=1000)
    blobs = tmp_path / "models--x--y" / "blobs"
    blobs.mkdir(parents=True)
    # finalized 600B blob + two stale partials for the SAME hash (must be ignored).
    (blobs / "aaaa").write_bytes(b"x" * 600)
    (blobs / "aaaa.11111111.incomplete").write_bytes(b"x" * 500)
    (blobs / "aaaa.22222222.incomplete").write_bytes(b"x" * 100)
    # in-progress blob: only partials (two retry attempts) → count the largest (350).
    (blobs / "bbbb.33333333.incomplete").write_bytes(b"x" * 200)
    (blobs / "bbbb.44444444.incomplete").write_bytes(b"x" * 350)
    (blobs / "cccc").write_bytes(b"x" * 50)  # a small finalized blob
    view = d.status_view("fast", "x/y")
    # deduped: 600 (aaaa) + 350 (largest bbbb partial) + 50 (cccc) = 1000, NOT the naive 1800.
    assert view["downloaded_bytes"] == 1000
    assert view["percent"] == 100.0  # never > 100


def test_status_view_percent_none_when_total_unknown(monkeypatch, tmp_path):
    # Total not yet known (HF lookup pending/failed) → percent is None even though bytes are on
    # disk, rather than a divide-by-zero or a bogus 0%.
    monkeypatch.setattr(dl, "HF_HUB_CACHE", str(tmp_path))
    d = dl.Downloads()
    d._states["fast"] = dl.DownloadState(status="downloading", total_bytes=0)
    blobs = tmp_path / "models--x--y" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "blob").write_bytes(b"z" * 42)
    view = d.status_view("fast", "x/y")
    assert view["downloaded_bytes"] == 42
    assert view["total_bytes"] == 0
    assert view["percent"] is None


def test_status_view_complete_reports_100_without_touching_disk():
    # A completed download is 100% by definition — no blobs walk needed (HF_HUB_CACHE untouched).
    d = dl.Downloads()
    d._states["fast"] = dl.DownloadState(status="complete", total_bytes=500, finished_at=1.0)
    view = d.status_view("fast", "x/y")
    assert view["percent"] == 100.0
    assert view["downloaded_bytes"] == 500


def test_status_view_idle_has_no_percent():
    d = dl.Downloads()
    view = d.status_view("never-touched", "x/y")
    assert view["status"] == "idle"
    assert view["percent"] is None
    assert view["downloaded_bytes"] == 0


def test_start_populates_total_bytes_from_hf_api(monkeypatch):
    """start() kicks off a best-effort HfApi lookup (on its own thread) that fills total_bytes
    from the repo's sibling sizes — the progress denominator. HfApi is monkeypatched so this is
    offline and deterministic (as the note in the task allows)."""
    class _Sibling:
        def __init__(self, size):
            self.size = size

    class _Info:
        siblings = [_Sibling(100), _Sibling(200), _Sibling(300)]

    class _Api:
        def model_info(self, repo_id, **kwargs):
            return _Info()

    monkeypatch.setattr(dl, "HfApi", _Api)
    monkeypatch.setattr(dl, "snapshot_download", lambda *a, **k: "/fake/path")
    d = dl.Downloads()
    d.start("fast", "some/repo")
    for _ in range(100):
        if d.status("fast").total_bytes:
            break
        time.sleep(0.02)
    assert d.status("fast").total_bytes == 600


def test_start_total_bytes_is_best_effort_on_hf_failure(monkeypatch):
    # If the HF metadata lookup raises, the download itself must still complete — total_bytes
    # just stays 0 (percent unknown), never an error.
    class _Api:
        def model_info(self, repo_id, **kwargs):
            raise RuntimeError("hub unreachable")

    monkeypatch.setattr(dl, "HfApi", _Api)
    monkeypatch.setattr(dl, "snapshot_download", lambda *a, **k: "/fake/path")
    d = dl.Downloads()
    d.start("fast", "some/repo")
    for _ in range(50):
        if d.status("fast").status != "downloading":
            break
        time.sleep(0.02)
    assert d.status("fast").status == "complete"
    assert d.status("fast").total_bytes == 0

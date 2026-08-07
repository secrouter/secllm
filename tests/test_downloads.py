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

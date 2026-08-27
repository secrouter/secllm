# Copyright 2026 Austin Probe
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for secllm.audit — the hash-chained JSONL admin-audit logger."""

from __future__ import annotations

import json

from secllm import audit as au


def test_disabled_logger_is_a_safe_noop(tmp_path):
    log = au.AuditLogger(tmp_path / "audit.jsonl", enabled=False)
    assert log.record("model.load", principal="alice") is None
    assert log.recent() == []
    assert log.verify() == {"ok": True, "checked": 0}
    assert not (tmp_path / "audit.jsonl").exists()


def test_record_writes_canonical_fields(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = au.AuditLogger(path, enabled=True)
    rec = log.record(
        "model.load", principal="alice", source_ip="10.0.0.1", target="Llama-3.2-3B-Instruct",
        outcome="ok", detail={"context_length": 4096},
    )
    assert rec["type"] == "model.load"
    assert rec["principal"] == "alice"
    assert rec["sourceIp"] == "10.0.0.1"
    assert rec["target"] == "Llama-3.2-3B-Instruct"
    assert rec["outcome"] == "ok"
    assert rec["detail"] == {"context_length": 4096}
    assert rec["prevHash"] == au.GENESIS
    assert rec["seq"] == 1
    assert isinstance(rec["hash"], str) and len(rec["hash"]) == 64
    # actually persisted, one JSON object per line
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == rec


def test_chain_links_successive_records(tmp_path):
    log = au.AuditLogger(tmp_path / "audit.jsonl", enabled=True)
    r1 = log.record("model.load", principal="alice", target="m1")
    r2 = log.record("model.unload", principal="alice", target="m1")
    r3 = log.record("model.load", principal="bob", target="m2")
    assert r1["prevHash"] == au.GENESIS
    assert r2["prevHash"] == r1["hash"]
    assert r3["prevHash"] == r2["hash"]
    assert [r["seq"] for r in (r1, r2, r3)] == [1, 2, 3]


def test_verify_ok_on_untouched_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = au.AuditLogger(path, enabled=True)
    for i in range(5):
        log.record("model.load", principal="alice", target=f"m{i}")
    result = log.verify()
    assert result == {"ok": True, "checked": 5}
    assert au.verify_chain(path) == result


def test_verify_detects_tampered_field(tmp_path):
    """Editing any field of a record (not just the hash) must be caught: the tamper changes the
    canonicalized body, so the stored hash no longer matches the recomputation."""
    path = tmp_path / "audit.jsonl"
    log = au.AuditLogger(path, enabled=True)
    log.record("model.load", principal="alice", target="m1")
    log.record("model.unload", principal="alice", target="m1")
    log.record("model.load", principal="bob", target="m2")

    lines = path.read_text().strip().splitlines()
    tampered = json.loads(lines[1])
    tampered["principal"] = "eve"  # attacker rewrites who did it — hash no longer matches
    lines[1] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n")

    result = au.verify_chain(path)
    assert result["ok"] is False
    assert result["brokenAtSeq"] == 2


def test_verify_detects_deleted_record_breaks_linkage(tmp_path):
    """Deleting a middle record breaks the prevHash chain of the record after it, even though
    neither remaining record was itself edited."""
    path = tmp_path / "audit.jsonl"
    log = au.AuditLogger(path, enabled=True)
    log.record("model.load", principal="alice", target="m1")
    log.record("model.unload", principal="alice", target="m1")
    log.record("model.load", principal="bob", target="m2")

    lines = path.read_text().strip().splitlines()
    del lines[1]  # remove the middle record entirely
    path.write_text("\n".join(lines) + "\n")

    result = au.verify_chain(path)
    assert result["ok"] is False
    assert result["brokenAtSeq"] == 3  # the record now immediately following the gap


def test_chain_continues_across_logger_restarts(tmp_path):
    """A fresh AuditLogger pointed at an existing log picks up prevHash/seq from the last
    record on disk, rather than restarting the chain at GENESIS."""
    path = tmp_path / "audit.jsonl"
    log1 = au.AuditLogger(path, enabled=True)
    r1 = log1.record("model.load", principal="alice", target="m1")

    log2 = au.AuditLogger(path, enabled=True)
    r2 = log2.record("model.unload", principal="alice", target="m1")
    assert r2["seq"] == 2
    assert r2["prevHash"] == r1["hash"]
    assert au.verify_chain(path) == {"ok": True, "checked": 2}


def test_recent_filters_and_orders_newest_first(tmp_path):
    log = au.AuditLogger(tmp_path / "audit.jsonl", enabled=True)
    log.record("model.load", principal="alice", target="m1")
    log.record("model.unload", principal="alice", target="m1")
    log.record("model.load", principal="bob", target="m2")

    all_events = log.recent()
    assert [e["seq"] for e in all_events] == [3, 2, 1]  # newest first

    loads_only = log.recent(type_filter="model.load")
    assert [e["seq"] for e in loads_only] == [3, 1]

    assert len(log.recent(limit=1)) == 1


def test_detail_is_metadata_only_never_secrets(tmp_path):
    """Detail is a small JSON-safe dict — the caller's responsibility to keep it metadata-only,
    but the logger itself must round-trip it faithfully (no silent truncation of legitimate
    metadata) and never invent a way for a secret to leak through some other field."""
    log = au.AuditLogger(tmp_path / "audit.jsonl", enabled=True)
    rec = log.record(
        "model.download", principal="token-admin", target="m1", outcome="error",
        detail={"repo_id": "org/repo", "phase": "failed", "error": "network is down"},
    )
    assert rec["detail"]["repo_id"] == "org/repo"
    assert "token" not in json.dumps(rec).lower().replace("token-admin", "")

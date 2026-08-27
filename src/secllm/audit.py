# Copyright 2026 Austin Probe
# SPDX-License-Identifier: Apache-2.0
"""Admin-plane audit logging (CMMC AU-3.3.1 / AU-3.3.8).

SecLLM's inference path (``/v1/*``) is deliberately NOT audited here — SecRouter sits in front
of it and owns per-user request governance (auth, policy, budgets, audit) by design; see
``docs/deploy.md`` ("Behind SecRouter") and ``docs/control-validation.md``. What this module
covers is the ADMIN PLANE: who loaded/unloaded/downloaded which model, and when an admin-gated
request was rejected.

Each record is one append-only JSONL line, chained with a SHA-256 hash over the record's own
canonicalized fields (sorted-keys JSON — order-independent, so hashing never depends on dict
insertion order) so any edit, deletion, or reordering of the log is detectable. The genesis
value is the literal string ``"GENESIS"`` (Spec B.2).

Canonical fields (Spec B.1): ``ts`` (ISO-8601 UTC), ``type`` (dotted lowercase, e.g.
``model.load``), ``principal`` (OIDC ``sub``, or ``"token-admin"`` for the static break-glass
token), ``sourceIp`` (optional), ``target`` (optional — the model id), ``outcome``, ``detail``
(a small JSON-safe object — METADATA ONLY, never prompt/response/weights content), ``prevHash``,
``hash``. A ``seq`` field is added for pagination/verify reporting.

Disabled (``SECLLM_AUDIT_ENABLED=false``) makes every call a no-op; writes never raise into the
caller — a logging failure degrades to stderr, never breaks an admin action.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "GENESIS"


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical(record: dict[str, Any]) -> str:
    """Stable serialization for hashing: sorted keys, compact separators — independent of the
    dict's insertion order, so re-building the same fields always hashes identically."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _harden_path(path: Path, mode: int) -> None:
    """Best-effort at-rest permission tightening (CMMC-2). Never raises — a chmod failure
    (e.g. an unsupported filesystem) must not break audit writes."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


@dataclass
class AuditRecord:
    seq: int
    ts: str
    type: str
    principal: str
    sourceIp: str | None
    target: str | None
    outcome: str
    detail: dict[str, Any]
    prevHash: str
    hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "ts": self.ts, "type": self.type, "principal": self.principal,
            "sourceIp": self.sourceIp, "target": self.target, "outcome": self.outcome,
            "detail": self.detail, "prevHash": self.prevHash, "hash": self.hash,
        }


class AuditLogger:
    """Append-only, hash-chained JSONL audit logger for SecLLM's admin plane.

    A disabled logger (``enabled=False``) is a safe no-op — every method returns ``None``/empty
    without touching disk. Writes are serialized with a lock; a write failure is logged to
    stderr and swallowed, never raised into the calling route.
    """

    def __init__(self, path: str | Path | None, *, enabled: bool = True) -> None:
        self.enabled = enabled and path is not None
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._seq = 0
        self._prev_hash = GENESIS
        if self.enabled and self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _harden_path(self._path.parent, 0o700)
            last_seq, last_hash = _last_record(self._path)
            self._seq = last_seq
            self._prev_hash = last_hash or GENESIS

    def record(
        self,
        type: str,  # noqa: A002 — "type" is the canonical field name (Spec B.1)
        *,
        principal: str,
        source_ip: str | None = None,
        target: str | None = None,
        outcome: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Append one audit record. Returns the written record (JSON-safe dict), or ``None``
        when the logger is disabled."""
        if not self.enabled:
            return None
        with self._lock:
            self._seq += 1
            rec = AuditRecord(
                seq=self._seq, ts=_utc_now_iso(), type=type, principal=principal,
                sourceIp=source_ip, target=target, outcome=outcome, detail=detail or {},
                prevHash=self._prev_hash,
            )
            body = rec.to_dict()
            del body["hash"]
            digest = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
            rec.hash = digest
            self._prev_hash = digest
            out = rec.to_dict()
            self._write(out)
            return out

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str)
        try:
            if self._path is not None:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                _harden_path(self._path, 0o600)
        except OSError as exc:  # never break an admin action on a logging failure
            print(f"AUDIT-ERROR could not write audit record: {exc}")  # noqa: T201

    def recent(self, *, limit: int = 100, type_filter: str | None = None) -> list[dict[str, Any]]:
        """The most recent ``limit`` records (newest first), optionally restricted to one
        ``type``. Reads the log fresh each call — simple and correct; the admin audit view is a
        low-QPS, low-volume surface."""
        if not self.enabled or self._path is None or not self._path.exists():
            return []
        matches: list[dict[str, Any]] = []
        with open(self._path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if type_filter and rec.get("type") != type_filter:
                    continue
                matches.append(rec)
        matches.reverse()
        return matches[:limit]

    def verify(self) -> dict[str, Any]:
        """Validate this logger's own chain. See :func:`verify_chain`."""
        if not self.enabled or self._path is None:
            return {"ok": True, "checked": 0}
        return verify_chain(self._path)


def _last_record(path: Path) -> tuple[int, str | None]:
    """The ``(seq, hash)`` of the last record in an existing log, to continue the chain across a
    restart. ``(0, None)`` if the log doesn't exist or is empty/unreadable."""
    if not path.exists():
        return 0, None
    last = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
    except OSError:
        return 0, None
    if not last:
        return 0, None
    try:
        rec = json.loads(last)
    except json.JSONDecodeError:
        return 0, None
    seq = rec.get("seq")
    digest = rec.get("hash")
    return (int(seq) if isinstance(seq, int) else 0,
            digest if isinstance(digest, str) else None)


def verify_chain(path: str | Path) -> dict[str, Any]:
    """Validate a JSONL audit log's hash chain.

    Returns ``{"ok": bool, "checked": N, "brokenAtSeq": seq}`` — ``brokenAtSeq`` present only
    when ``ok`` is False. Detects both an edited record (recomputed hash mismatch) and broken
    linkage (a record's ``prevHash`` not matching the previous record's ``hash``)."""
    p = Path(path)
    if not p.exists():
        return {"ok": True, "checked": 0}
    prev = GENESIS
    checked = 0
    with open(p, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "checked": checked, "brokenAtSeq": None}
            checked += 1
            seq = rec.get("seq")
            stored_hash = rec.get("hash")
            if stored_hash is None or rec.get("prevHash") != prev:
                return {"ok": False, "checked": checked, "brokenAtSeq": seq}
            body = {k: v for k, v in rec.items() if k != "hash"}
            recomputed = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
            if recomputed != stored_hash:
                return {"ok": False, "checked": checked, "brokenAtSeq": seq}
            prev = stored_hash
    return {"ok": True, "checked": checked}


def get_audit_logger(config) -> AuditLogger:
    """Build an :class:`AuditLogger` from ``Config`` + env. Enabled by default — the admin plane
    is a small, low-volume surface, so audit is on unless an operator opts out via
    ``SECLLM_AUDIT_ENABLED=false``. Path defaults under the service's own data dir (state,
    gitignored, not shipped in the image) and can be overridden with ``SECLLM_AUDIT_PATH`` for
    e.g. an operator-provided protected/SIEM-forwarded volume."""
    enabled = os.environ.get("SECLLM_AUDIT_ENABLED", "true").strip().lower() not in ("false", "0", "")
    raw_path = os.environ.get("SECLLM_AUDIT_PATH", "").strip()
    path = Path(raw_path) if raw_path else (config.data_dir / "audit.jsonl")
    return AuditLogger(path, enabled=enabled)

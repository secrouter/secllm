# Copyright 2026 Austin Probe
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the admin-plane audit wiring: routes emit events with the right
principal, the audit/verify/evidence endpoints are admin-gated, and the evidence bundle never
leaks token values."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from httpx import ASGITransport

ADMIN = {"Authorization": "Bearer test-token"}


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_audit_and_evidence_endpoints_require_admin(stack):
    app, ctx = stack
    async with _client(app) as c:
        assert (await c.get("/admin/api/audit")).status_code == 401
        assert (await c.get("/admin/api/audit/verify")).status_code == 401
        assert (await c.get("/admin/api/evidence")).status_code == 401

        assert (await c.get("/admin/api/audit", headers=ADMIN)).status_code == 200
        assert (await c.get("/admin/api/audit/verify", headers=ADMIN)).status_code == 200
        assert (await c.get("/admin/api/evidence", headers=ADMIN)).status_code == 200


async def test_rejected_admin_attempt_emits_auth_failure_with_no_principal_leak(stack):
    app, ctx = stack
    async with _client(app) as c:
        r = await c.get("/admin/api/models", headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401

    events = ctx.audit.recent(type_filter="auth.failure")
    assert len(events) == 1
    assert events[0]["principal"] == "anonymous"
    assert events[0]["outcome"] == "deny"
    # the bad token itself must never be recorded
    assert "wrong-token" not in json.dumps(events[0])


async def test_break_glass_token_yields_token_admin_principal(stack):
    app, ctx = stack
    async with _client(app) as c:
        r = await c.post("/admin/api/models/Llama-3.2-3B-Instruct/load", headers=ADMIN)
        assert r.status_code == 200
    ctx.supervisor.stop_all()

    events = ctx.audit.recent(type_filter="model.load")
    assert len(events) == 1
    assert events[0]["principal"] == "token-admin"
    assert events[0]["target"] == "Llama-3.2-3B-Instruct"
    assert events[0]["outcome"] == "ok"


async def test_load_unload_reload_emit_chained_audit_events(stack):
    app, ctx = stack
    async with _client(app) as c:
        r = await c.post("/admin/api/models/Llama-3.2-3B-Instruct/load", headers=ADMIN)
        assert r.status_code == 200
        r = await c.post("/admin/api/models/Llama-3.2-3B-Instruct/reload", headers=ADMIN)
        assert r.status_code == 200
        r = await c.post("/admin/api/models/Llama-3.2-3B-Instruct/unload", headers=ADMIN)
        assert r.status_code == 200
    ctx.supervisor.stop_all()

    types = [e["type"] for e in reversed(ctx.audit.recent(limit=10))]
    assert types == ["model.load", "model.reload", "model.unload"]
    verify = ctx.audit.verify()
    assert verify["ok"] is True
    assert verify["checked"] == 3


async def test_load_failure_on_unknown_model_is_audited_as_error(stack):
    app, ctx = stack
    async with _client(app) as c:
        r = await c.post("/admin/api/models/does-not-exist/load", headers=ADMIN)
        assert r.status_code == 404

    events = ctx.audit.recent(type_filter="model.load")
    assert len(events) == 1
    assert events[0]["outcome"] == "error"
    assert events[0]["target"] == "does-not-exist"


async def test_download_started_and_completed_are_audited(stack, monkeypatch):
    app, ctx = stack

    def fake_snapshot_download(repo_id, **kwargs):
        return "/fake/path"

    monkeypatch.setattr("secllm.downloads.snapshot_download", fake_snapshot_download)
    async with _client(app) as c:
        r = await c.post("/admin/api/models/Llama-3.2-3B-Instruct/download", headers=ADMIN)
        assert r.status_code == 200

    for _ in range(50):
        events = ctx.audit.recent(type_filter="model.download")
        if any(e["detail"].get("phase") == "completed" for e in events):
            break
        await asyncio.sleep(0.02)

    events = ctx.audit.recent(type_filter="model.download")
    phases = {e["detail"].get("phase") for e in events}
    assert "started" in phases and "completed" in phases
    started = next(e for e in events if e["detail"].get("phase") == "started")
    assert started["principal"] == "token-admin"
    assert started["sourceIp"] is not None


async def test_evidence_bundle_shape_and_no_secrets_leaked(stack):
    app, ctx = stack
    async with _client(app) as c:
        await c.post("/admin/api/models/Llama-3.2-3B-Instruct/load", headers=ADMIN)
        r = await c.get("/admin/api/evidence", headers=ADMIN)
    ctx.supervisor.stop_all()

    assert r.status_code == 200
    body = r.json()
    assert body["product"] == "secllm"
    assert "generatedAt" in body and "generatedBy" in body
    assert body["generatedBy"] == "token-admin"
    assert set(body["config"].keys()) >= {"backend", "models", "auth"}
    assert body["auditChain"]["ok"] is True
    assert isinstance(body["auditRecent"], list) and len(body["auditRecent"]) >= 1
    assert isinstance(body["controls"], list) and len(body["controls"]) >= 3
    control_ids = {(c_["family"], c_["id"]) for c_ in body["controls"]}
    assert ("AU", "3.3.1") in control_ids
    assert ("AU", "3.3.8") in control_ids
    assert ("IA", "3.5.2") in control_ids
    assert any(c_["id"] == "delegation" for c_ in body["controls"])

    # the admin token (the actual bearer credential used above) must never appear anywhere
    dump = json.dumps(body)
    assert ctx.config.admin_token not in dump
    assert "test-token" not in dump
    assert "SECLLM_API_TOKEN" not in dump or "api_token_configured" in dump


async def test_audit_verify_endpoint_matches_logger(stack):
    app, ctx = stack
    async with _client(app) as c:
        await c.post("/admin/api/models/Llama-3.2-3B-Instruct/load", headers=ADMIN)
        r = await c.get("/admin/api/audit/verify", headers=ADMIN)
    ctx.supervisor.stop_all()
    assert r.status_code == 200
    assert r.json() == ctx.audit.verify()


async def test_audit_endpoint_limit_and_type_filter(stack):
    app, ctx = stack
    async with _client(app) as c:
        await c.post("/admin/api/models/Llama-3.2-3B-Instruct/load", headers=ADMIN)
        await c.post("/admin/api/models/Llama-3.2-3B-Instruct/unload", headers=ADMIN)
        r = await c.get("/admin/api/audit?type=model.load&limit=5", headers=ADMIN)
    assert r.status_code == 200
    events = r.json()["events"]
    assert all(e["type"] == "model.load" for e in events)
    assert len(events) == 1

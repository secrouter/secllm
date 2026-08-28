# Copyright 2026 Austin Probe
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for catalog management: hot-reload (POST /admin/api/catalog/reload),
write-through CRUD (GET/PUT/DELETE /admin/api/catalog/models/{id}), the built-in-catalog 409
guard, orphaned-worker flagging, and the audit trail (catalog.reload / catalog.changed).

Uses its own fixture (``catalog_stack``) rather than conftest's ``stack`` because these tests
need a REAL ``models.json`` on disk (``SECLLM_CATALOG``) to exercise write-through — conftest's
``stack`` intentionally stays on the built-in catalog for every pre-existing test. The built-in
409 tests reuse conftest's ``stack`` (no SECLLM_CATALOG set == built-in, exactly what they need
to assert)."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

ADMIN = {"Authorization": "Bearer test-token"}


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture
def catalog_stack(tmp_path, monkeypatch):
    """Same shape as conftest's ``stack``, but backed by a real, editable ``models.json`` —
    two entries (``m1``, ``m2``) — so catalog CRUD/reload/write-through can be exercised."""
    monkeypatch.setenv("SECLLM_BACKEND", "mock")
    monkeypatch.setenv("SECLLM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SECLLM_ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("SECLLM_HEALTH_TIMEOUT", "2")
    monkeypatch.setenv("SECLLM_STARTUP_GRACE", "30")
    monkeypatch.setenv("SECLLM_WORKER_PORT_BASE", "12900")

    catalog_path = tmp_path / "models.json"
    catalog_path.write_text(json.dumps({
        "models": [
            {"id": "m1", "hf_model": "org/m1", "vram_fraction": 0.2},
            {"id": "m2", "hf_model": "org/m2", "name": "Model Two"},
        ],
    }, indent=2) + "\n")
    monkeypatch.setenv("SECLLM_CATALOG", str(catalog_path))

    from secllm.admin.api import build_router as admin_router
    from secllm.audit import get_audit_logger
    from secllm.catalog import Catalog
    from secllm.config import Config
    from secllm.context import Context
    from secllm.downloads import Downloads
    from secllm.health import HealthMonitor
    from secllm.stats import Stats
    from secllm.supervisor import Supervisor

    cfg = Config.from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog.load(cfg.catalog_path)
    supervisor = Supervisor(cfg, catalog)
    health = HealthMonitor(cfg, supervisor)
    ctx = Context(config=cfg, catalog=catalog, supervisor=supervisor, health=health,
                  downloads=Downloads(), stats=Stats(), audit=get_audit_logger(cfg))

    app = FastAPI()
    app.include_router(admin_router(ctx))
    try:
        yield app, ctx, catalog_path
    finally:
        supervisor.stop_all()


# ---- GET /admin/api/catalog ---------------------------------------------------------------


async def test_get_catalog_requires_admin(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        assert (await c.get("/admin/api/catalog")).status_code == 401
        assert (await c.get("/admin/api/catalog", headers=ADMIN)).status_code == 200


async def test_get_catalog_reports_source_and_editable(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        r = await c.get("/admin/api/catalog", headers=ADMIN)
    body = r.json()
    assert body["source"] == str(path)
    assert body["built_in"] is False
    assert body["count"] == 2
    assert {m["id"] for m in body["models"]} == {"m1", "m2"}


async def test_get_catalog_reports_builtin(stack):
    # conftest's `stack` never sets SECLLM_CATALOG — the built-in catalog.
    app, ctx = stack
    async with _client(app) as c:
        r = await c.get("/admin/api/catalog", headers=ADMIN)
    body = r.json()
    assert body["built_in"] is True
    assert body["source"] is None


# ---- built-in catalog is read-only (409, never silently materializes a file) ---------------


async def test_upsert_on_builtin_catalog_is_409(stack):
    app, ctx = stack
    async with _client(app) as c:
        r = await c.put("/admin/api/catalog/models/new-model", headers=ADMIN,
                         json={"hf_model": "org/new"})
    assert r.status_code == 409
    assert "SECLLM_CATALOG" in r.json()["detail"]


async def test_delete_on_builtin_catalog_is_409(stack):
    app, ctx = stack
    async with _client(app) as c:
        r = await c.delete("/admin/api/catalog/models/Llama-3.2-3B-Instruct", headers=ADMIN)
    assert r.status_code == 409


# ---- PUT (upsert) ---------------------------------------------------------------------------


async def test_upsert_new_entry_writes_file_and_hot_swaps(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        r = await c.put("/admin/api/catalog/models/m3", headers=ADMIN,
                         json={"hf_model": "org/m3", "vram_fraction": 0.3, "revision": "v9"})
    assert r.status_code == 200
    body = r.json()
    assert body["model"]["hf_model"] == "org/m3"
    assert body["model"]["revision"] == "v9"
    assert set(body["diff"]) >= {"id", "hf_model", "vram_fraction", "revision"}

    # hot-swapped into the SAME in-process catalog (no restart, no re-fetch needed)
    assert ctx.catalog.get("m3") is not None
    assert ctx.catalog.get("m3").hf_model == "org/m3"

    # write-through: the file on disk actually has it
    on_disk = json.loads(path.read_text())
    ids = {m["id"] for m in on_disk["models"]}
    assert ids == {"m1", "m2", "m3"}


async def test_upsert_existing_entry_diff_reports_only_changed_field_names(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        r = await c.put("/admin/api/catalog/models/m1", headers=ADMIN,
                         json={"hf_model": "org/m1", "vram_fraction": 0.9})
    assert r.status_code == 200
    assert r.json()["diff"] == ["vram_fraction"]  # only the field that actually changed
    assert ctx.catalog.get("m1").vram_fraction == 0.9


async def test_upsert_rejects_id_mismatch(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        r = await c.put("/admin/api/catalog/models/m1", headers=ADMIN,
                         json={"id": "not-m1", "hf_model": "org/m1"})
    assert r.status_code == 400


async def test_upsert_rejects_invalid_entry(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        r = await c.put("/admin/api/catalog/models/bad", headers=ADMIN,
                         json={"hf_model": "org/bad", "vram_fraction": 5.0})
    assert r.status_code == 400
    assert any("vram_fraction" in e for e in r.json()["detail"]["errors"])
    # rejected — never actually written
    on_disk = json.loads(path.read_text())
    assert "bad" not in {m["id"] for m in on_disk["models"]}


async def test_upsert_rejects_unknown_key(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        r = await c.put("/admin/api/catalog/models/bad2", headers=ADMIN,
                         json={"hf_model": "org/bad2", "made_up_field": 1})
    assert r.status_code == 400
    assert any("unknown key" in e for e in r.json()["detail"]["errors"])


async def test_upsert_preserves_other_entries_formatting(catalog_stack):
    app, ctx, path = catalog_stack
    before = path.read_text()
    async with _client(app) as c:
        await c.put("/admin/api/catalog/models/m3", headers=ADMIN, json={"hf_model": "org/m3"})
    after = json.loads(path.read_text())
    m1_entry = next(m for m in after["models"] if m["id"] == "m1")
    m2_entry = next(m for m in after["models"] if m["id"] == "m2")
    # m1/m2 completely untouched — same fields, same values, same key order.
    assert list(m1_entry) == ["id", "hf_model", "vram_fraction"]
    assert m1_entry == {"id": "m1", "hf_model": "org/m1", "vram_fraction": 0.2}
    assert list(m2_entry) == ["id", "hf_model", "name"]
    assert m2_entry == {"id": "m2", "hf_model": "org/m2", "name": "Model Two"}
    assert after["models"][0]["id"] == "m1" and after["models"][1]["id"] == "m2"  # order kept
    assert before.endswith("\n")


# ---- DELETE -----------------------------------------------------------------------------------


async def test_delete_removes_entry_and_hot_swaps(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        r = await c.delete("/admin/api/catalog/models/m2", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {"id": "m2", "deleted": True}
    assert ctx.catalog.get("m2") is None
    on_disk = json.loads(path.read_text())
    assert {m["id"] for m in on_disk["models"]} == {"m1"}


async def test_delete_unknown_model_404(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        r = await c.delete("/admin/api/catalog/models/does-not-exist", headers=ADMIN)
    assert r.status_code == 404


async def test_delete_of_loaded_model_leaves_worker_running_but_orphaned(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        load_r = await c.post("/admin/api/models/m1/load", headers=ADMIN)
        assert load_r.status_code == 200
        del_r = await c.delete("/admin/api/catalog/models/m1", headers=ADMIN)
        assert del_r.status_code == 200

        list_r = await c.get("/admin/api/models", headers=ADMIN)
    rows = list_r.json()["models"]
    orphan = next(m for m in rows if m["id"] == "m1")
    assert orphan["orphaned"] is True
    assert orphan["loaded"] is True
    assert ctx.supervisor.get("m1") is not None  # still actually running
    assert ctx.supervisor.get("m1").process_alive()


# ---- POST /admin/api/catalog/reload ----------------------------------------------------------


async def test_reload_picks_up_added_entry_without_touching_running_worker(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        load_r = await c.post("/admin/api/models/m1/load", headers=ADMIN)
        assert load_r.status_code == 200
        worker_before = ctx.supervisor.get("m1")
        assert worker_before is not None

        # Edit the file OUT OF BAND (as if hand-edited or CRUD'd from elsewhere), then reload.
        raw = json.loads(path.read_text())
        raw["models"].append({"id": "m3", "hf_model": "org/m3"})
        path.write_text(json.dumps(raw, indent=2) + "\n")

        r = await c.post("/admin/api/catalog/reload", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == ["m3"]
    assert body["removed"] == []
    assert body["count"] == 3
    assert ctx.catalog.get("m3") is not None

    # The already-running m1 worker is the SAME object — reload never touches Supervisor.
    worker_after = ctx.supervisor.get("m1")
    assert worker_after is worker_before
    assert worker_after.process_alive()


async def test_reload_detects_removed_entry_and_flags_worker_orphaned(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        await c.post("/admin/api/models/m1/load", headers=ADMIN)

        raw = json.loads(path.read_text())
        raw["models"] = [m for m in raw["models"] if m["id"] != "m1"]
        path.write_text(json.dumps(raw, indent=2) + "\n")

        r = await c.post("/admin/api/catalog/reload", headers=ADMIN)
        assert r.json()["removed"] == ["m1"]

        list_r = await c.get("/admin/api/models", headers=ADMIN)
    row = next(m for m in list_r.json()["models"] if m["id"] == "m1")
    assert row["orphaned"] is True
    assert ctx.supervisor.get("m1").process_alive()  # left running


async def test_reload_changed_entry_is_reported(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        raw = json.loads(path.read_text())
        for m in raw["models"]:
            if m["id"] == "m2":
                m["vram_fraction"] = 0.7
        path.write_text(json.dumps(raw, indent=2) + "\n")

        r = await c.post("/admin/api/catalog/reload", headers=ADMIN)
    assert r.json()["changed"] == ["m2"]
    assert ctx.catalog.get("m2").vram_fraction == 0.7


async def test_reload_invalid_file_is_400_and_audited_as_error(catalog_stack):
    app, ctx, path = catalog_stack
    path.write_text(json.dumps({"models": [{"name": "no id or hf_model"}]}))
    async with _client(app) as c:
        r = await c.post("/admin/api/catalog/reload", headers=ADMIN)
    assert r.status_code == 400
    events = ctx.audit.recent(type_filter="catalog.reload")
    assert len(events) == 1
    assert events[0]["outcome"] == "error"
    # the in-memory catalog is untouched by a failed reload
    assert ctx.catalog.get("m1") is not None


# ---- audit trail ------------------------------------------------------------------------------


async def test_catalog_changed_audit_never_contains_full_entry_values(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        await c.put("/admin/api/catalog/models/m1", headers=ADMIN,
                     json={"hf_model": "org/m1", "vram_fraction": 0.99})
        await c.delete("/admin/api/catalog/models/m2", headers=ADMIN)

    events = ctx.audit.recent(type_filter="catalog.changed")
    assert len(events) == 2
    for e in events:
        assert e["outcome"] == "ok"
        assert isinstance(e["detail"]["diff"], list)
        assert all(isinstance(f, str) for f in e["detail"]["diff"])
        # never a raw value like 0.99 leaking into the audit trail
        assert "0.99" not in json.dumps(e)
    upsert_evt = next(e for e in events if e["detail"]["action"] == "upsert")
    delete_evt = next(e for e in events if e["detail"]["action"] == "delete")
    assert upsert_evt["target"] == "m1"
    assert delete_evt["target"] == "m2"

    verify = ctx.audit.verify()
    assert verify["ok"] is True


async def test_catalog_reload_audit_records_added_removed_changed(catalog_stack):
    app, ctx, path = catalog_stack
    async with _client(app) as c:
        r = await c.post("/admin/api/catalog/reload", headers=ADMIN)
    assert r.status_code == 200
    events = ctx.audit.recent(type_filter="catalog.reload")
    assert len(events) == 1
    assert events[0]["outcome"] == "ok"
    assert events[0]["detail"] == {"added": [], "removed": [], "changed": []}

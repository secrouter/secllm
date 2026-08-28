# Copyright 2026 Austin Probe
# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for the admin console's single-page HTML (src/secllm/admin/ui.py) — not a
browser test (no JS engine here), just: the page renders via the real route, the catalog
section exists with the ids the JS depends on, and every pre-existing id/class the console
already relied on is still intact (a regression here would silently break the console without
any Python-side test ever failing)."""

from __future__ import annotations

import re

from secllm.admin.ui import CONSOLE_HTML

ADMIN = {"Authorization": "Bearer test-token"}


def test_console_html_has_the_catalog_section():
    for needle in (
        'id="catalogCard"', 'id="catalogHint"', 'id="catalogList"', 'id="catalogAdd"',
        'id="catalogReloadBtn"', 'id="catalogFormWrap"', 'id="catalogForm"',
        'id="catalogCancelBtn"', 'id="catalogFormErr"',
    ):
        assert needle in CONSOLE_HTML, f"missing {needle}"
    # the real schema fields, not a stand-in name like "repo_id"
    for field_id in (
        "cf-id", "cf-name", "cf-hf_model", "cf-mlx_model", "cf-revision",
        "cf-vram_fraction", "cf-context_length", "cf-tool_call_parser",
        "cf-vllm_args", "cf-sampling_override",
    ):
        assert f'id="{field_id}"' in CONSOLE_HTML, f"missing form field {field_id}"


def test_console_html_preserves_pre_existing_ids_and_classes():
    # These predate the catalog feature — a regression here breaks the console's existing
    # load/unload/reload flow, sign-in, or theming, not just the new catalog card.
    for needle in (
        'id="pill"', 'id="models"', 'id="connect"', 'id="token"', 'id="signin"',
        'id="signout"', 'id="who"', 'id="info"', 'id="themeBtn"',
        'class="model"', 'class="badge', 'class="pill"', 'class="ghost',
        'class="danger"', 'function act(', 'function refresh(',
    ):
        assert needle in CONSOLE_HTML, f"missing pre-existing {needle}"


def test_console_html_script_is_syntactically_valid_javascript():
    """No JS engine to fully execute the page here, but at minimum every <script> block must
    parse — a stray brace/quote in the catalog JS would silently break the ENTIRE console
    (including the pre-existing load/unload/reload flow that shares the same <script> tag),
    with no Python-side signal. Uses Node if available; skipped otherwise (CI/dev environments
    without Node stay green, matching how test_mlx_server.py skips without mlx_lm)."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available in this environment")

    scripts = re.findall(r"<script>(.*?)</script>", CONSOLE_HTML, re.S)
    assert len(scripts) >= 2  # the pre-paint theme snippet + the main app script
    main_script = max(scripts, key=len)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(main_script)
        path = f.name
    result = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def test_admin_console_route_serves_the_catalog_section(stack):
    import httpx
    from httpx import ASGITransport

    app, ctx = stack
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="catalogList"' in r.text
    assert 'id="models"' in r.text  # pre-existing section still there alongside it

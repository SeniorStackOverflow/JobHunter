from hashlib import sha256
from pathlib import Path

import httpx

from app.admin.routes import _admin_asset_url
from app.main import app


def test_admin_javascript_uses_in_app_confirmation_dialog() -> None:
    script = Path("app/admin/static/admin.js").read_text(encoding="utf-8")

    assert "window.confirm" not in script
    assert "window.alert" not in script
    assert ".showModal()" in script
    assert "data-confirm-reason" in script
    assert "dialog.returnValue = '';" in script


def test_admin_javascript_initializes_every_custom_control() -> None:
    script = Path("app/admin/static/admin.js").read_text(encoding="utf-8")

    for hook in (
        "data-theme-toggle",
        "data-menu-toggle",
        "data-profile-select",
        "data-password-toggle",
        "data-notice-dismiss",
        "data-confirm-dialog",
    ):
        assert hook in script


def test_admin_templates_do_not_use_inline_event_handlers() -> None:
    templates = Path("app/admin/templates")

    for template in templates.glob("*.html"):
        markup = template.read_text(encoding="utf-8").casefold()
        assert " onclick=" not in markup
        assert " onchange=" not in markup
        assert " onsubmit=" not in markup


def test_admin_assets_use_content_versioned_urls() -> None:
    script = Path("app/admin/static/admin.js")
    expected_version = sha256(script.read_bytes()).hexdigest()[:16]

    assert _admin_asset_url("admin.js") == f"/admin-assets/admin.js?v={expected_version}"
    base = Path("app/admin/templates/base.html").read_text(encoding="utf-8")
    assert "admin_asset_url('admin.js')" in base
    assert "admin_asset_url('favicon.svg')" in base


async def test_admin_asset_cache_headers_match_versioned_urls() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        versioned = await client.get(_admin_asset_url("admin.js"))
        unversioned = await client.get("/admin-assets/admin.js")

    assert versioned.status_code == 200
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert unversioned.status_code == 200
    assert unversioned.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"

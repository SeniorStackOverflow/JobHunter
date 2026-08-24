from hashlib import sha256
from pathlib import Path

import httpx

from app.admin.routes import (
    _admin_asset_url,
    _application_approval_issue,
    _daily_application_rules,
)
from app.main import app


def test_admin_javascript_uses_in_app_confirmation_dialog() -> None:
    script = Path("app/admin/static/admin.js").read_text(encoding="utf-8")

    assert "window.confirm" not in script
    assert "window.alert" not in script
    assert ".showModal()" in script
    assert "data-confirm-reason" in script
    assert "dialog.returnValue = '';" in script


def test_admin_daily_application_rules_preserve_advanced_settings() -> None:
    existing = {"verified_only": True, "minimum_daily_applications": 9}

    enabled = _daily_application_rules(existing, minimum=2, force_minimum=True)
    disabled = _daily_application_rules(existing, minimum=0, force_minimum=True)

    assert enabled == {
        "verified_only": True,
        "minimum_daily_applications": 2,
        "force_minimum_daily_applications": True,
    }
    assert disabled["verified_only"] is True
    assert disabled["force_minimum_daily_applications"] is False
    assert existing == {"verified_only": True, "minimum_daily_applications": 9}


def test_admin_explains_stale_review_even_when_letter_was_validated() -> None:
    application = {
        "content_validated": True,
        "policy_result": {
            "safe_stop_reason": "match_evaluation_stale",
            "rules_failed": ["match_evaluation_current"],
        },
    }

    assert _application_approval_issue(application) == (
        "Вакансия изменилась — JobHunter выполняет повторный анализ."
    )


def test_admin_explains_markerless_legacy_stale_review() -> None:
    application = {
        "content_validated": True,
        "policy_result": {
            "decision": "auto_approved",
            "rules_failed": [],
        },
        "match_evaluation_issue": "match_evaluation_stale",
    }

    assert _application_approval_issue(application) == (
        "Вакансия изменилась — JobHunter выполняет повторный анализ."
    )


def test_admin_javascript_initializes_every_custom_control() -> None:
    script = Path("app/admin/static/admin.js").read_text(encoding="utf-8")

    for hook in (
        "data-theme-toggle",
        "data-menu-toggle",
        "data-profile-select",
        "data-daily-limit-range",
        "data-password-toggle",
        "data-notice-dismiss",
        "data-confirm-dialog",
    ):
        assert hook in script


def test_admin_javascript_disables_dormant_daily_minimum() -> None:
    script = Path("app/admin/static/admin.js").read_text(encoding="utf-8")

    assert "minimumInput.readOnly = !enabled;" in script
    assert "minimumInput.setAttribute('aria-disabled', String(!enabled));" in script
    assert "forceInput.checked &&" in script
    assert "forceInput.addEventListener('change', syncMinimumAvailability);" in script
    assert "syncMinimumAvailability();" in script


def test_admin_preferences_use_independent_columns() -> None:
    markup = Path("app/admin/templates/dashboard_settings.html").read_text(encoding="utf-8")
    styles = Path("app/admin/templates/base.html").read_text(encoding="utf-8")

    assert 'class="preference-columns full"' in markup
    assert markup.count('class="preference-column"') == 2
    assert ".preference-column{display:grid;gap:15px;align-content:start}" in styles
    assert ".form-grid,.preference-columns{grid-template-columns:1fr}" in styles


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

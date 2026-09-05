from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.admin.routes import (
    _admin_asset_url,
    _application_approval_issue,
    _daily_application_rules,
    _phone_health,
)
from app.main import app
from app.models.entities import PhoneChannelHealth
from app.models.enums import PhoneComponentStatus


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


@pytest.mark.asyncio
async def test_phone_health_aggregates_components(sqlite_session_factory: Any) -> None:
    from app.settings import get_settings

    async with sqlite_session_factory() as session:
        session.add(
            PhoneChannelHealth(
                component="phonegate_transport",
                status=PhoneComponentStatus.HEALTHY,
                detail=None,
                last_ok_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        session.add(
            PhoneChannelHealth(
                component="a14_daemon",
                status=PhoneComponentStatus.DEGRADED,
                detail="ADB fallback",
                last_ok_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()

        result = await _phone_health(session)

        assert result["channel"] == "degraded"
        assert len(result["components"]) == 2
        assert result["components"][0]["component"] == "a14_daemon"
        assert result["components"][1]["component"] == "phonegate_transport"
        assert result["configured"] == get_settings().phone_agent_enabled


@pytest.mark.asyncio
async def test_phone_health_downgrades_stale_agent(sqlite_session_factory: Any) -> None:
    async with sqlite_session_factory() as session:
        session.add(
            PhoneChannelHealth(
                component="phonegate_transport",
                status=PhoneComponentStatus.HEALTHY,
                last_ok_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        session.add(
            PhoneChannelHealth(
                component="agent",
                status=PhoneComponentStatus.HEALTHY,
                detail="poll ok",
                last_ok_at=datetime.now(UTC) - timedelta(hours=2),
                updated_at=datetime.now(UTC) - timedelta(hours=2),
            )
        )
        await session.commit()

        result = await _phone_health(session)

    agent = next(c for c in result["components"] if c["component"] == "agent")
    assert agent["status"] == "unavailable"
    assert result["channel"] == "unavailable"


@pytest.mark.asyncio
async def test_phone_health_device_block_includes_updated_at(sqlite_session_factory: Any) -> None:
    """F4b: the device line rendered 'never' because the snapshot's updated_at
    lives on its own column, not in the payload. _phone_health must expose it."""
    from app.models.entities import PhoneDeviceSnapshot

    stamp = datetime.now(UTC) - timedelta(minutes=5)
    async with sqlite_session_factory() as session:
        session.add(
            PhoneDeviceSnapshot(
                id="current",
                payload={"daemon_version": "0.2.1", "battery": 87, "sim_operator": "Orange"},
                updated_at=stamp,
            )
        )
        await session.commit()
        result = await _phone_health(session)

    got = result["device"]["updated_at"]
    assert isinstance(got, datetime)
    # SQLite drops tzinfo on read; compare the wall-clock value
    assert got.replace(tzinfo=None) == stamp.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_phone_health_auto_answer_block(sqlite_session_factory: Any, monkeypatch) -> None:
    from app.admin import routes as admin_routes
    from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY
    from tests.fixtures.fake_redis import FakeAsyncRedis

    redis = FakeAsyncRedis()
    await redis.set(AUTO_ANSWER_STOPPED_KEY, "1")
    monkeypatch.setattr(admin_routes, "_phone_redis", lambda: redis)

    async with sqlite_session_factory() as session:
        result = await _phone_health(session)
    assert result["auto_answer"]["stopped"] is True
    assert "enabled" in result["auto_answer"]


@pytest.mark.asyncio
async def test_phone_health_tolerates_malformed_call_owned_key(
    sqlite_session_factory: Any, monkeypatch
) -> None:
    """A corrupted CALL_OWNED_KEY value must degrade, not 500 the diagnostics view —
    same "diagnostic endpoint must not crash" principle as the Redis-unreachable case."""
    from app.admin import routes as admin_routes
    from app.phone.orchestrator import CALL_OWNED_KEY
    from tests.fixtures.fake_redis import FakeAsyncRedis

    redis = FakeAsyncRedis()
    await redis.set(CALL_OWNED_KEY, "not-a-uuid")
    monkeypatch.setattr(admin_routes, "_phone_redis", lambda: redis)

    async with sqlite_session_factory() as session:
        result = await _phone_health(session)
    assert result["active_call"] is None


def test_phone_health_template_exists_and_uses_correct_variables() -> None:
    template_path = Path("app/admin/templates/_phone_health.html")
    assert template_path.exists()
    markup = template_path.read_text(encoding="utf-8")
    assert "phone_health.components" in markup
    assert "status_tone" in markup

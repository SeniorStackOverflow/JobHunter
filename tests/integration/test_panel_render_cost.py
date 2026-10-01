from __future__ import annotations

# Russian labels are assertions against the rendered interface.
import pytest

import app.ui.dashboard as dashboard
from app.security.auth import SessionSigner
from tests.integration.test_panel_controls import PanelScenario
from tests.integration.test_panel_controls import panel_scenario as panel_scenario
from tests.integration.test_user_invite_auth import user_auth_context as user_auth_context

pytestmark = pytest.mark.integration


def _count_calls(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    calls: list[str] = []
    original = getattr(dashboard, name)

    async def counting(*args, **kwargs):
        calls.append(name)
        return await original(*args, **kwargs)

    monkeypatch.setattr(dashboard, name, counting)
    return calls


@pytest.mark.parametrize("view", ["decisions", "history", "settings", "calls"])
async def test_non_overview_views_skip_overview_only_diagnostics(
    panel_scenario: PanelScenario, monkeypatch: pytest.MonkeyPatch, view: str
) -> None:
    context = panel_scenario.context
    context.client.cookies.set(
        context.settings.session_cookie_name,
        SessionSigner(context.settings.secret_key.get_secret_value()).issue(
            context.settings.admin_username
        ),
    )
    audit_calls = _count_calls(monkeypatch, "daily_minimum_audit")
    backlog_calls = _count_calls(monkeypatch, "count_profile_matching_backlog")

    page = await context.client.get(
        "/admin", params={"view": view, "profile_id": str(panel_scenario.primary_id)}
    )

    assert page.status_code == 200
    # The header quota is shown on every view and must stay correct ...
    assert 'aria-label="Отправлено сегодня 1 из 20"' in page.text
    # ... without replaying the whole evaluation history on every page load.
    assert audit_calls == []
    assert backlog_calls == []


async def test_overview_still_renders_daily_target_diagnostics(
    panel_scenario: PanelScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = panel_scenario.context
    context.client.cookies.set(
        context.settings.session_cookie_name,
        SessionSigner(context.settings.secret_key.get_secret_value()).issue(
            context.settings.admin_username
        ),
    )
    audit_calls = _count_calls(monkeypatch, "daily_minimum_audit")
    backlog_calls = _count_calls(monkeypatch, "count_profile_matching_backlog")

    page = await context.client.get(
        "/admin", params={"view": "overview", "profile_id": str(panel_scenario.primary_id)}
    )

    assert page.status_code == 200
    assert 'aria-label="Отправлено сегодня 1 из 20"' in page.text
    assert audit_calls == ["daily_minimum_audit"]
    assert backlog_calls == ["count_profile_matching_backlog"]


async def _overview_with_minimum(
    panel_scenario: PanelScenario, *, minimum: int, auto_send_enabled: bool
) -> str:
    from sqlalchemy import select

    from app.models.entities import JobPreference

    context = panel_scenario.context
    async with context.session_factory() as session:
        preference = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == panel_scenario.primary_id)
        )
        assert preference is not None
        preference.additional_rules = {"minimum_daily_applications": minimum}
        preference.auto_send_enabled = auto_send_enabled
        preference.global_pause = False
        await session.commit()
    page = await context.client.get(
        "/admin", params={"view": "overview", "profile_id": str(panel_scenario.primary_id)}
    )
    assert page.status_code == 200
    return page.text


async def test_overview_replays_history_only_while_a_deficit_is_shown(
    panel_scenario: PanelScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = panel_scenario.context
    context.client.cookies.set(
        context.settings.session_cookie_name,
        SessionSigner(context.settings.secret_key.get_secret_value()).issue(
            context.settings.admin_username
        ),
    )
    replay_flags: list[bool] = []
    original = dashboard.daily_minimum_audit

    async def recording(*args, **kwargs):
        replay_flags.append(kwargs.get("include_replay", True))
        return await original(*args, **kwargs)

    monkeypatch.setattr(dashboard, "daily_minimum_audit", recording)

    # One confirmed send today: a minimum of 1 is met, a minimum of 3 is not.
    met = await _overview_with_minimum(panel_scenario, minimum=1, auto_send_enabled=True)
    assert "Минимум выполнен" in met
    paused = await _overview_with_minimum(panel_scenario, minimum=3, auto_send_enabled=False)
    assert "Автоматическая отправка приостановлена" in paused
    deficit = await _overview_with_minimum(panel_scenario, minimum=3, auto_send_enabled=True)
    assert "Осталось добрать 2" in deficit
    # The deficit breakdown is the only consumer of the full history replay.
    assert replay_flags == [False, False, True]

from __future__ import annotations

import pytest

from app.phone import agent as agent_module
from app.settings.config import Settings, get_settings


@pytest.mark.asyncio
async def test_run_exits_zero_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings, "cache_clear", lambda: None, raising=False)
    monkeypatch.setattr(agent_module, "get_settings", lambda: Settings(_env_file=None))
    code = await agent_module.run()
    assert code == 0


def test_cli_registers_phone_agent_subcommand() -> None:
    from app.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["phone-agent"])
    assert args.command == "phone-agent"

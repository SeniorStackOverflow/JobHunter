from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app import deployment
from app.observability import health as health_module
from app.settings import Settings
from app.ui.presentation import templates


@pytest.mark.parametrize("environment", ["development", "production", "test"])
def test_all_base_pages_identify_dev_without_marking_other_environments(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    settings = Settings(environment="test", app_revision="candidate-123").model_copy(
        update={"environment": environment}
    )
    monkeypatch.setattr(deployment, "get_settings", lambda: settings)
    request = Request({"type": "http", "method": "GET", "path": "/login", "headers": []})
    # Every panel, login and invitation page inherits this common shell.
    html = templates.get_template("base.html").render(request=request)
    if environment == "development":
        assert "[DEV] JobHunter" in html
        assert 'aria-label="DEV версия"' in html
        assert "candidate-123" in html
    else:
        assert "[DEV]" not in html
        assert "dev-banner" not in html


@pytest.mark.asyncio
async def test_dev_identity_reaches_health_and_unauthenticated_api_errors(monkeypatch):
    settings = Settings(environment="development", app_revision="candidate-123")
    monkeypatch.setattr(deployment, "get_settings", lambda: settings)
    monkeypatch.setattr(health_module, "get_settings", lambda: settings)
    app = FastAPI()
    app.add_middleware(deployment.DeploymentIdentityMiddleware)
    app.include_router(health_module.router)

    @app.get("/protected")
    async def protected():
        raise HTTPException(status_code=401)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dev.test"
    ) as client:
        health = await client.get("/health")
        assert health.json() == {
            "status": "ok",
            "environment": "development",
            "revision": "candidate-123",
        }
        unauthorized = await client.get("/protected")
        assert unauthorized.status_code == 401
        missing = await client.get("/missing")
        assert missing.status_code == 404
        for response in (health, unauthorized, missing):
            assert response.headers["X-JobHunter-Environment"] == "development"
            assert response.headers["X-JobHunter-Revision"] == "candidate-123"


def test_revision_cannot_inject_http_headers():
    with pytest.raises(ValidationError):
        Settings(app_revision="candidate\r\nInjected: value")

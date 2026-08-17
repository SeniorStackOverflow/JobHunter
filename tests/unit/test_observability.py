from __future__ import annotations

from typing import Any, cast

from app.observability.logging import sanitize_log_event


def test_log_sanitizer_redacts_nested_secrets_oauth_queries_and_uri_credentials() -> None:
    event = {
        "event": (
            "GET /api/v1/oauth/gmail/callback?code=oauth-code&state=public-state "
            "postgresql://operator:database-password@database/job_agent"
        ),
        "authorization": "Bearer secret-access-token",
        "provider": {"refresh_token": "secret-refresh-token"},
    }

    sanitized = sanitize_log_event(cast(Any, None), "info", event)
    serialized = repr(sanitized)

    assert "oauth-code" not in serialized
    assert "database-password" not in serialized
    assert "secret-access-token" not in serialized
    assert "secret-refresh-token" not in serialized
    assert "code=[redacted]" in serialized
    assert "operator:[redacted]@database" in serialized

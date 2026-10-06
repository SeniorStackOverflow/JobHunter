"""Public deployment identity shared by browser, API and MCP surfaces."""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.settings import Settings, get_settings


def deployment_identity(settings: Settings | None = None) -> dict[str, str | bool]:
    current = settings or get_settings()
    return {
        "environment": current.environment,
        "is_dev": current.environment == "development",
        "revision": current.app_revision,
        "label": "DEV" if current.environment == "development" else current.environment.upper(),
    }


class DeploymentIdentityMiddleware:
    """Mark every HTTP response, including auth failures and early rate limits."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()

        async def send_identified(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-JobHunter-Environment"] = settings.environment
                headers["X-JobHunter-Revision"] = settings.app_revision
            await send(message)

        await self.app(scope, receive, send_identified)

# ruff: noqa: E501
from __future__ import annotations

import importlib.util
import os
import secrets
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

# OAuth metadata and the embedded standalone login document contain deliberately
# indivisible URLs/HTML attributes.
TOKEN_FILE = Path(
    os.getenv("JOBHUNTER_MCP_OAUTH_TOKEN_FILE", "/home/andrei/.config/jobhunter-mcp/oauth-token")
)


def _shared_password() -> str:
    """Reuse the same operator password as the existing MCP OAuth pool without duplicating it here."""
    env_value = os.getenv("MCP_SECRET_PASSWORD")
    if env_value:
        return env_value
    spec = importlib.util.spec_from_file_location(
        "existing_playwright_oauth", "/home/andrei/playwright_oauth_server.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load existing MCP OAuth password source")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = getattr(module, "SECRET_PASSWORD", "")
    if not isinstance(value, str) or not value:
        raise RuntimeError("existing MCP OAuth password is unavailable")
    return value


def _access_token() -> str:
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("JobHunter MCP OAuth token file is empty")
    return token


SECRET_PASSWORD = _shared_password()
auth_codes: dict[str, bool | dict[str, str]] = {}


async def openid_config(request):
    host = request.headers.get("host", "jobhunter.46-225-103-75.sslip.io")
    base_url = f"https://{host}"
    return JSONResponse(
        {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "registration_endpoint": f"{base_url}/oauth/register",
            "userinfo_endpoint": f"{base_url}/oauth/userinfo",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
                "none",
            ],
            "scopes_supported": ["openid", "profile", "email"],
            "code_challenge_methods_supported": ["S256"],
        }
    )


async def protected_resource_config(request):
    host = request.headers.get("host", "jobhunter.46-225-103-75.sslip.io")
    base_url = f"https://{host}"
    return JSONResponse(
        {
            "resource": f"{base_url}/mcp",
            "authorization_servers": [base_url],
            "scopes_supported": ["openid", "profile", "email"],
            "bearer_methods_supported": ["header"],
        }
    )


async def register_endpoint(request):
    return JSONResponse(
        {
            "client_id": "chatgpt-jobhunter-mcp",
            "client_secret": "chatgpt-jobhunter-mcp-public-client",
            "client_id_issued_at": 1786570000,
            "client_secret_expires_at": 0,
        },
        status_code=201,
    )


async def userinfo_endpoint(request):
    return JSONResponse(
        {
            "sub": "andrei",
            "name": "Andrei",
        }
    )


async def authorize_get(request):
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    client_id = request.query_params.get("client_id", "")
    html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>JobHunter MCP Auth</title></head>
<body style='font-family:sans-serif;background:#0f172a;color:#fff;padding:50px;text-align:center;'>
<div style='background:#1e293b;padding:30px;border-radius:12px;display:inline-block;width:340px;'>
<h2 style='color:#38bdf8;'>JobHunter MCP</h2>
<p>Authorize ChatGPT to use your JobHunter MCP.</p>
<form method='POST' action='/oauth/authorize'>
<input type='hidden' name='redirect_uri' value='{redirect_uri}'>
<input type='hidden' name='state' value='{state}'>
<input type='hidden' name='client_id' value='{client_id}'>
<input type='password' name='password' placeholder='Password' style='width:90%;padding:10px;margin:10px 0;' required autofocus><br>
<button type='submit' style='padding:10px 20px;background:#0284c7;color:#fff;border:none;border-radius:5px;cursor:pointer;'>Authorize JobHunter</button>
</form></div></body></html>"""
    return HTMLResponse(html)


async def authorize_post(request):
    form = await request.form()
    password = str(form.get("password", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    state = str(form.get("state", ""))
    if not secrets.compare_digest(password, SECRET_PASSWORD):
        return HTMLResponse(
            "<h3 style='color:red;text-align:center;'>Invalid password</h3>", status_code=401
        )
    code = secrets.token_urlsafe(32)
    auth_codes[code] = True
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}code={code}&state={state}", status_code=302)


async def token_endpoint(request):
    form = await request.form() if request.method == "POST" else request.query_params
    code = str(form.get("code", ""))
    if code in auth_codes:
        del auth_codes[code]
        return JSONResponse(
            {
                "access_token": _access_token(),
                "token_type": "Bearer",
                "expires_in": 3600 * 24 * 365,
            }
        )
    return JSONResponse({"error": "invalid_grant"}, status_code=400)


app = Starlette(
    routes=[
        Route("/.well-known/oauth-authorization-server", openid_config, methods=["GET"]),
        Route("/.well-known/openid-configuration", openid_config, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", protected_resource_config, methods=["GET"]),
        Route(
            "/.well-known/oauth-protected-resource/{path:path}",
            protected_resource_config,
            methods=["GET"],
        ),
        Route("/oauth/authorize", authorize_get, methods=["GET"]),
        Route("/oauth/authorize", authorize_post, methods=["POST"]),
        Route("/oauth/token", token_endpoint, methods=["GET", "POST"]),
        Route("/oauth/register", register_endpoint, methods=["GET", "POST"]),
        Route("/oauth/userinfo", userinfo_endpoint, methods=["GET", "POST"]),
    ]
)

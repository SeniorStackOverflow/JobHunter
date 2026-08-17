from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.security.auth import verify_api_key
from app.settings import get_settings


async def require_api_actor(authorization: str | None = Header(default=None)) -> str:
    settings = get_settings()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not settings.mcp_api_keys_hashed or not verify_api_key(token, settings.mcp_api_keys_hashed):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid API credential")
    return "api-key"

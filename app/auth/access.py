"""Shared browser session and account ownership checks for user routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Account, UserProfile
from app.models.enums import AccountStatus
from app.profiles.service import ProfileService
from app.security.auth import AccountSessionSigner, CsrfProtector, SessionSigner
from app.settings import Settings, get_settings


def settings() -> Settings:
    return get_settings()


def require_user_feature(*, registration: bool = False) -> None:
    current = settings()
    enabled = current.invite_registration_enabled if registration else current.user_accounts_enabled
    if not enabled:
        raise HTTPException(status_code=404)


def account_signer() -> AccountSessionSigner:
    return AccountSessionSigner(settings().secret_key.get_secret_value())


def csrf() -> CsrfProtector:
    return CsrfProtector(settings().secret_key.get_secret_value())


def secure_cookie() -> bool:
    return settings().public_base_url.casefold().startswith("https://")


def has_admin_session(request: Request) -> bool:
    current = settings()
    token = request.cookies.get(current.session_cookie_name)
    if not token:
        return False
    subject = SessionSigner(current.secret_key.get_secret_value()).verify(
        token, current.session_ttl_seconds
    )
    return subject == current.admin_username


async def current_account(request: Request, session: AsyncSession) -> tuple[Account, str] | None:
    current = settings()
    token = request.cookies.get(current.user_session_cookie_name)
    if not token:
        return None
    payload = account_signer().verify(token, current.user_session_ttl_seconds)
    if payload is None:
        return None
    account = await session.get(Account, payload.account_id)
    if (
        account is None
        or account.status != AccountStatus.ACTIVE
        or account.session_version != payload.version
    ):
        return None
    return account, token


async def require_account(request: Request, session: AsyncSession) -> tuple[Account, str]:
    current = await current_account(request, session)
    if current is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    return current


def require_user_csrf(request: Request, csrf_token: str) -> None:
    token = request.cookies.get(settings().user_session_cookie_name)
    if not token or not csrf().verify(csrf_token, token, settings().csrf_ttl_seconds):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


async def require_owned_profile(
    session: AsyncSession, account_id: UUID, profile_id: UUID
) -> UserProfile:
    profile = await ProfileService().get_profile(session, profile_id, owner_account_id=account_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return profile


__all__ = [
    "account_signer",
    "csrf",
    "current_account",
    "has_admin_session",
    "require_account",
    "require_owned_profile",
    "require_user_csrf",
    "require_user_feature",
    "secure_cookie",
    "settings",
]

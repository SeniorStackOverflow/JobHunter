from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Account, AccountIdentity, Invite, JobPreference, UserProfile
from app.models.enums import AccountRole, AccountStatus, IdentityProvider, ProfileStatus

INVITE_PREFIX = "jhi_"
DEFAULT_INVITE_TTL = timedelta(days=14)
DEFAULT_OPEN_INVITE_TTL = timedelta(days=2)


class AccountAccessError(ValueError):
    pass


class InviteError(ValueError):
    pass


class InviteInvalid(InviteError):
    pass


class InviteUnavailable(InviteError):
    pass


class InviteEmailMismatch(InviteError):
    pass


class IdentityAlreadyRegistered(InviteError):
    pass


@dataclass(frozen=True, slots=True)
class CreatedInvite:
    invite: Invite
    token: str


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def _hash_invite_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse_invite_token(token: str) -> tuple[UUID, str]:
    if not token.startswith(INVITE_PREFIX):
        raise InviteInvalid("invalid invitation")
    identifier, separator, secret = token[len(INVITE_PREFIX) :].partition(".")
    if not separator or not secret:
        raise InviteInvalid("invalid invitation")
    try:
        invite_id = UUID(identifier)
    except ValueError as exc:
        raise InviteInvalid("invalid invitation") from exc
    if len(secret) < 32:
        raise InviteInvalid("invalid invitation")
    return invite_id, secret


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def invite_state(invite: Invite, *, now: datetime | None = None) -> str:
    current = _utc(now) if now is not None else datetime.now(UTC)
    if invite.redeemed_at is not None:
        return "redeemed"
    if invite.revoked_at is not None:
        return "revoked"
    if _utc(invite.expires_at) <= current:
        return "expired"
    return "active"


class AccountService:
    async def get_active_account(self, session: AsyncSession, account_id: UUID) -> Account:
        account = await session.get(Account, account_id)
        if account is None or account.status != AccountStatus.ACTIVE:
            raise AccountAccessError("account is not active")
        return account

    async def identity_account(
        self,
        session: AsyncSession,
        *,
        provider: IdentityProvider,
        subject: str,
    ) -> Account | None:
        return await session.scalar(
            select(Account)
            .join(AccountIdentity, AccountIdentity.account_id == Account.id)
            .where(
                AccountIdentity.provider == provider,
                AccountIdentity.subject == subject,
            )
        )

    async def authenticate_google_identity(
        self,
        session: AsyncSession,
        *,
        subject: str,
        email: str,
        email_verified: bool,
    ) -> Account:
        if not subject or not email_verified:
            raise AccountAccessError("verified Google identity required")
        identity = await session.scalar(
            select(AccountIdentity).where(
                AccountIdentity.provider == IdentityProvider.GOOGLE,
                AccountIdentity.subject == subject,
            )
        )
        if identity is None:
            raise AccountAccessError("Google identity is not registered")
        account = await session.get(Account, identity.account_id)
        if account is None or account.status != AccountStatus.ACTIVE:
            raise AccountAccessError("account is not active")
        identity.email = normalize_email(email)
        identity.email_verified = True
        identity.last_login_at = datetime.now(UTC)
        await session.flush()
        return account

    async def list_owned_profiles(
        self,
        session: AsyncSession,
        account_id: UUID,
        *,
        include_archived: bool = False,
    ) -> list[UserProfile]:
        conditions = [UserProfile.owner_account_id == account_id]
        if not include_archived:
            conditions.append(UserProfile.status != ProfileStatus.ARCHIVED)
        return list(
            (
                await session.scalars(
                    select(UserProfile)
                    .where(*conditions)
                    .order_by(UserProfile.created_at, UserProfile.id)
                )
            ).all()
        )

    async def get_owned_profile(
        self,
        session: AsyncSession,
        *,
        account_id: UUID,
        profile_id: UUID,
    ) -> UserProfile | None:
        return await session.scalar(
            select(UserProfile).where(
                UserProfile.id == profile_id,
                UserProfile.owner_account_id == account_id,
            )
        )

    async def suspend(self, session: AsyncSession, account_id: UUID) -> Account:
        account = await session.scalar(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        if account is None:
            raise AccountAccessError("account does not exist")
        account.status = AccountStatus.SUSPENDED
        account.session_version += 1
        now = datetime.now(UTC)
        for invite in (
            await session.scalars(
                select(Invite).where(
                    Invite.created_by_account_id == account.id,
                    Invite.redeemed_at.is_(None),
                    Invite.revoked_at.is_(None),
                    Invite.expires_at > now,
                )
            )
        ).all():
            invite.revoked_at = now
        owned_profile_ids = select(UserProfile.id).where(
            UserProfile.owner_account_id == account.id
        )
        await session.execute(
            update(JobPreference)
            .where(JobPreference.profile_id.in_(owned_profile_ids))
            .values(global_pause=True)
        )
        await session.flush()
        return account


class InviteService:
    async def _locked_creator(self, session: AsyncSession, account_id: UUID) -> Account:
        account = await session.scalar(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        if account is None or account.status != AccountStatus.ACTIVE:
            raise InviteUnavailable("invitation creator is not active")
        return account

    async def _used_allowance(self, session: AsyncSession, account_id: UUID) -> int:
        now = datetime.now(UTC)
        return int(
            await session.scalar(
                select(func.count(Invite.id)).where(
                    Invite.created_by_account_id == account_id,
                    or_(
                        Invite.redeemed_at.is_not(None),
                        and_(
                            Invite.redeemed_at.is_(None),
                            Invite.revoked_at.is_(None),
                            Invite.expires_at > now,
                        ),
                    ),
                )
            )
            or 0
        )

    async def create(
        self,
        session: AsyncSession,
        *,
        creator_account_id: UUID,
        target_email: str | None,
        ttl: timedelta | None = None,
    ) -> CreatedInvite:
        creator = await self._locked_creator(session, creator_account_id)
        normalized_target = normalize_email(target_email) if target_email else None
        if normalized_target is None and not (
            creator.role == AccountRole.ADMIN or creator.allow_open_invites
        ):
            raise InviteUnavailable("open invitations are not allowed")
        if creator.role != AccountRole.ADMIN:
            used = await self._used_allowance(session, creator.id)
            if used >= creator.invite_allowance:
                raise InviteUnavailable("invitation allowance exhausted")
        effective_ttl = ttl or (
            DEFAULT_OPEN_INVITE_TTL if normalized_target is None else DEFAULT_INVITE_TTL
        )
        if effective_ttl <= timedelta(0):
            raise InviteUnavailable("invitation expiry must be in the future")
        secret = secrets.token_urlsafe(32)
        invite = Invite(
            created_by_account_id=creator.id,
            secret_hash=_hash_invite_secret(secret),
            target_email=normalized_target,
            expires_at=datetime.now(UTC) + effective_ttl,
        )
        session.add(invite)
        await session.flush()
        return CreatedInvite(invite=invite, token=f"{INVITE_PREFIX}{invite.id}.{secret}")

    async def validate(self, session: AsyncSession, token: str) -> Invite:
        invite_id, secret = parse_invite_token(token)
        invite = await session.get(Invite, invite_id)
        if invite is None or not hmac.compare_digest(
            invite.secret_hash, _hash_invite_secret(secret)
        ):
            raise InviteInvalid("invalid invitation")
        if invite_state(invite) != "active":
            raise InviteUnavailable("invitation is not available")
        return invite

    async def _redeem_locked_invite(
        self,
        session: AsyncSession,
        *,
        invite: Invite,
        subject: str,
        email: str,
        email_verified: bool,
    ) -> Account:
        if not subject or not email_verified:
            raise InviteInvalid("verified Google identity required")
        if invite_state(invite) != "active":
            raise InviteUnavailable("invitation is not available")
        normalized_email = normalize_email(email)
        if invite.target_email is not None and invite.target_email != normalized_email:
            raise InviteEmailMismatch("invitation belongs to a different email address")
        existing_identity = await session.scalar(
            select(AccountIdentity.id).where(
                AccountIdentity.provider == IdentityProvider.GOOGLE,
                AccountIdentity.subject == subject,
            )
        )
        if existing_identity is not None:
            raise IdentityAlreadyRegistered("Google identity already registered")
        account = Account(
            role=AccountRole.USER,
            status=AccountStatus.ACTIVE,
            session_version=0,
            invite_allowance=0,
            allow_open_invites=False,
            max_profiles=1,
            allow_phone=False,
        )
        session.add(account)
        await session.flush()
        session.add(
            AccountIdentity(
                account_id=account.id,
                provider=IdentityProvider.GOOGLE,
                subject=subject,
                email=normalized_email,
                email_verified=True,
                last_login_at=datetime.now(UTC),
            )
        )
        invite.redeemed_at = datetime.now(UTC)
        invite.redeemed_by_account_id = account.id
        await session.flush()
        return account

    async def redeem_google_identity(
        self,
        session: AsyncSession,
        *,
        token: str,
        subject: str,
        email: str,
        email_verified: bool,
    ) -> Account:
        invite_id, secret = parse_invite_token(token)
        invite = await session.scalar(
            select(Invite).where(Invite.id == invite_id).with_for_update()
        )
        if invite is None or not hmac.compare_digest(
            invite.secret_hash, _hash_invite_secret(secret)
        ):
            raise InviteInvalid("invalid invitation")
        return await self._redeem_locked_invite(
            session,
            invite=invite,
            subject=subject,
            email=email,
            email_verified=email_verified,
        )

    async def redeem_bound_invite(
        self,
        session: AsyncSession,
        *,
        invite_id: UUID,
        subject: str,
        email: str,
        email_verified: bool,
    ) -> Account:
        """Redeem an invite after the raw token was exchanged for a signed server binding."""
        invite = await session.scalar(
            select(Invite).where(Invite.id == invite_id).with_for_update()
        )
        if invite is None:
            raise InviteInvalid("invitation does not exist")
        return await self._redeem_locked_invite(
            session,
            invite=invite,
            subject=subject,
            email=email,
            email_verified=email_verified,
        )

    async def revoke(
        self,
        session: AsyncSession,
        *,
        invite_id: UUID,
        actor_account_id: UUID,
    ) -> Invite:
        invite = await session.scalar(
            select(Invite).where(Invite.id == invite_id).with_for_update()
        )
        if invite is None:
            raise InviteInvalid("invitation does not exist")
        actor = await session.get(Account, actor_account_id)
        if actor is None or (
            actor.role != AccountRole.ADMIN and invite.created_by_account_id != actor.id
        ):
            raise InviteUnavailable("invitation cannot be revoked by this account")
        if invite.redeemed_at is not None:
            raise InviteUnavailable("redeemed invitation cannot be revoked")
        if invite.revoked_at is None:
            invite.revoked_at = datetime.now(UTC)
            await session.flush()
        return invite


__all__ = [
    "AccountAccessError",
    "AccountService",
    "CreatedInvite",
    "IdentityAlreadyRegistered",
    "InviteEmailMismatch",
    "InviteError",
    "InviteInvalid",
    "InviteService",
    "InviteUnavailable",
    "invite_state",
    "normalize_email",
    "parse_invite_token",
]

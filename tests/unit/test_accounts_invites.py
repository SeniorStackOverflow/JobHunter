from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.accounts import (
    AccountService,
    IdentityAlreadyRegistered,
    InviteEmailMismatch,
    InviteService,
    InviteUnavailable,
)
from app.models.entities import Account, AccountIdentity, Invite
from app.models.enums import AccountRole, AccountStatus, IdentityProvider
from app.profiles import ProfileService
from app.profiles.schemas import UserProfileInput


@pytest.mark.asyncio
async def test_personal_invite_redeems_once_and_creates_no_profile(
    sqlite_session_factory,
) -> None:
    service = InviteService()
    async with sqlite_session_factory() as session:
        admin = Account(
            role=AccountRole.ADMIN,
            status=AccountStatus.ACTIVE,
            allow_open_invites=True,
            max_profiles=100,
        )
        session.add(admin)
        await session.flush()
        created = await service.create(
            session,
            creator_account_id=admin.id,
            target_email="User@Example.COM",
        )
        await session.commit()

        assert created.token.startswith(f"jhi_{created.invite.id}.")
        assert created.invite.secret_hash not in created.token
        assert created.invite.target_email == "user@example.com"

    async with sqlite_session_factory() as session:
        account = await service.redeem_google_identity(
            session,
            token=created.token,
            subject="google-sub-1",
            email="USER@example.com",
            email_verified=True,
        )
        await session.commit()
        account_id = account.id

    async with sqlite_session_factory() as session:
        account = await session.get(Account, account_id)
        identity = await session.scalar(
            select(AccountIdentity).where(AccountIdentity.account_id == account_id)
        )
        profiles = await AccountService().list_owned_profiles(session, account_id)
        invite = await session.get(Invite, created.invite.id)

        assert account is not None
        assert account.role == AccountRole.USER
        assert account.invite_allowance == 0
        assert account.allow_open_invites is False
        assert identity is not None
        assert identity.provider == IdentityProvider.GOOGLE
        assert identity.email == "user@example.com"
        assert profiles == []
        assert invite is not None
        assert invite.redeemed_by_account_id == account_id

        with pytest.raises(InviteUnavailable):
            await service.redeem_google_identity(
                session,
                token=created.token,
                subject="google-sub-2",
                email="user@example.com",
                email_verified=True,
            )


@pytest.mark.asyncio
async def test_personal_invite_rejects_wrong_verified_email(
    sqlite_session_factory,
) -> None:
    service = InviteService()
    async with sqlite_session_factory() as session:
        admin = Account(
            role=AccountRole.ADMIN,
            status=AccountStatus.ACTIVE,
            allow_open_invites=True,
            max_profiles=100,
        )
        session.add(admin)
        await session.flush()
        created = await service.create(
            session,
            creator_account_id=admin.id,
            target_email="expected@example.com",
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        with pytest.raises(InviteEmailMismatch):
            await service.redeem_google_identity(
                session,
                token=created.token,
                subject="wrong-email-sub",
                email="other@example.com",
                email_verified=True,
            )


@pytest.mark.asyncio
async def test_invite_allowance_reserves_active_slots_and_revoke_releases_one(
    sqlite_session_factory,
) -> None:
    service = InviteService()
    async with sqlite_session_factory() as session:
        trusted = Account(
            role=AccountRole.USER,
            status=AccountStatus.ACTIVE,
            invite_allowance=1,
            allow_open_invites=False,
        )
        session.add(trusted)
        await session.flush()

        first = await service.create(
            session,
            creator_account_id=trusted.id,
            target_email="first@example.com",
        )
        with pytest.raises(InviteUnavailable):
            await service.create(
                session,
                creator_account_id=trusted.id,
                target_email="second@example.com",
            )
        with pytest.raises(InviteUnavailable):
            await service.create(
                session,
                creator_account_id=trusted.id,
                target_email=None,
            )

        await service.revoke(
            session,
            invite_id=first.invite.id,
            actor_account_id=trusted.id,
        )
        second = await service.create(
            session,
            creator_account_id=trusted.id,
            target_email="second@example.com",
        )
        await session.commit()

        assert second.invite.id != first.invite.id


@pytest.mark.asyncio
async def test_redeemed_invite_consumes_allowance_permanently(
    sqlite_session_factory,
) -> None:
    service = InviteService()
    async with sqlite_session_factory() as session:
        trusted = Account(
            role=AccountRole.USER,
            status=AccountStatus.ACTIVE,
            invite_allowance=1,
        )
        session.add(trusted)
        await session.flush()
        created = await service.create(
            session,
            creator_account_id=trusted.id,
            target_email="one@example.com",
        )
        await session.commit()
        trusted_id = trusted.id

    async with sqlite_session_factory() as session:
        await service.redeem_google_identity(
            session,
            token=created.token,
            subject="one-sub",
            email="one@example.com",
            email_verified=True,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        with pytest.raises(InviteUnavailable):
            await service.create(
                session,
                creator_account_id=trusted_id,
                target_email="two@example.com",
            )


@pytest.mark.asyncio
async def test_existing_google_identity_cannot_consume_new_invite(
    sqlite_session_factory,
) -> None:
    service = InviteService()
    async with sqlite_session_factory() as session:
        admin = Account(role=AccountRole.ADMIN, status=AccountStatus.ACTIVE)
        existing = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add_all([admin, existing])
        await session.flush()
        session.add(
            AccountIdentity(
                account_id=existing.id,
                provider=IdentityProvider.GOOGLE,
                subject="existing-sub",
                email="existing@example.com",
                email_verified=True,
            )
        )
        created = await service.create(
            session,
            creator_account_id=admin.id,
            target_email="existing@example.com",
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        with pytest.raises(IdentityAlreadyRegistered):
            await service.redeem_google_identity(
                session,
                token=created.token,
                subject="existing-sub",
                email="existing@example.com",
                email_verified=True,
            )


@pytest.mark.asyncio
async def test_profile_defaults_are_scoped_to_owner(sqlite_session_factory) -> None:
    profiles = ProfileService()
    async with sqlite_session_factory() as session:
        first = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE, max_profiles=2)
        second = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE, max_profiles=2)
        session.add_all([first, second])
        await session.flush()

        first_a = await profiles.create_profile(
            session,
            UserProfileInput(name="first-a"),
            owner_account_id=first.id,
            make_default=True,
        )
        first_b = await profiles.create_profile(
            session,
            UserProfileInput(name="first-b"),
            owner_account_id=first.id,
        )
        second_a = await profiles.create_profile(
            session,
            UserProfileInput(name="second-a"),
            owner_account_id=second.id,
            make_default=True,
        )
        await profiles.set_default_profile(
            session,
            first_b.id,
            owner_account_id=first.id,
        )
        await session.commit()

        assert first_a.is_default is False
        assert first_b.is_default is True
        assert second_a.is_default is True
        assert (
            await profiles.get_profile(
                session,
                second_a.id,
                owner_account_id=first.id,
            )
            is None
        )


@pytest.mark.asyncio
async def test_suspend_revokes_sessions_and_active_invites(
    sqlite_session_factory,
) -> None:
    invites = InviteService()
    accounts = AccountService()
    async with sqlite_session_factory() as session:
        trusted = Account(
            role=AccountRole.USER,
            status=AccountStatus.ACTIVE,
            session_version=4,
            invite_allowance=2,
        )
        session.add(trusted)
        await session.flush()
        created = await invites.create(
            session,
            creator_account_id=trusted.id,
            target_email="pending@example.com",
            ttl=timedelta(days=1),
        )
        suspended = await accounts.suspend(session, trusted.id)
        await session.commit()

        assert suspended.status == AccountStatus.SUSPENDED
        assert suspended.session_version == 5
        assert created.invite.revoked_at is not None

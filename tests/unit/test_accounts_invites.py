from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.accounts import (
    AccountService,
    IdentityAlreadyRegistered,
    InviteEmailMismatch,
    InviteService,
    InviteUnavailable,
)
from app.email.service import EmailSendBlocked, EmailService
from app.models.entities import (
    Account,
    AccountIdentity,
    Application,
    Invite,
    JobPreference,
    Resume,
    UserProfile,
)
from app.models.enums import (
    AccountRole,
    AccountStatus,
    ApplicationStatus,
    IdentityProvider,
    ProfileStatus,
)
from app.profiles import ProfileService
from app.profiles.schemas import UserProfileInput
from app.settings import Settings


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


@pytest.mark.asyncio
async def test_processing_profiles_require_active_account_and_profile(
    sqlite_session_factory,
) -> None:
    profiles = ProfileService()
    async with sqlite_session_factory() as session:
        active_account = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        suspended_account = Account(role=AccountRole.USER, status=AccountStatus.SUSPENDED)
        session.add_all([active_account, suspended_account])
        await session.flush()
        active = UserProfile(
            owner_account_id=active_account.id,
            status=ProfileStatus.ACTIVE,
            name="active",
        )
        draft = UserProfile(
            owner_account_id=active_account.id,
            status=ProfileStatus.DRAFT,
            name="draft",
        )
        suspended = UserProfile(
            owner_account_id=suspended_account.id,
            status=ProfileStatus.ACTIVE,
            name="suspended-owner",
        )
        unready = UserProfile(
            owner_account_id=active_account.id,
            status=ProfileStatus.ACTIVE,
            name="active-without-resume",
        )
        session.add_all([active, draft, suspended, unready])
        await session.flush()
        session.add(
            Resume(
                profile_id=active.id,
                name="Ready CV",
                category="office",
                storage_key="ready.pdf",
                original_filename="ready.pdf",
                mime_type="application/pdf",
                sha256="a" * 64,
                active=True,
                verified=True,
                is_default=True,
            )
        )
        await session.commit()

        processing = await profiles.list_processing_profiles(session)
        assert [item.id for item in processing] == [active.id]
        assert await profiles.get_processing_profile(session, active.id) is not None
        assert await profiles.get_processing_profile(session, draft.id) is None
        assert await profiles.get_processing_profile(session, suspended.id) is None
        assert await profiles.get_processing_profile(session, unready.id) is None


@pytest.mark.asyncio
async def test_suspend_also_pauses_owned_job_preferences(
    sqlite_session_factory,
) -> None:
    accounts = AccountService()
    async with sqlite_session_factory() as session:
        account = Account(
            role=AccountRole.USER,
            status=AccountStatus.ACTIVE,
            session_version=2,
        )
        session.add(account)
        await session.flush()
        profile = UserProfile(
            owner_account_id=account.id,
            status=ProfileStatus.ACTIVE,
            name="owner",
        )
        session.add(profile)
        await session.flush()
        preference = JobPreference(
            profile_id=profile.id,
            auto_send_enabled=True,
            global_pause=False,
        )
        session.add(preference)
        await session.commit()

        await accounts.suspend(session, account.id)
        await session.commit()

        stored = await session.get(JobPreference, preference.id)
        assert stored is not None
        assert stored.auto_send_enabled is True
        assert stored.global_pause is True


@pytest.mark.asyncio
async def test_email_send_blocks_suspended_owner_before_provider_or_dependencies(
    sqlite_session_factory,
    tmp_path,
) -> None:
    async with sqlite_session_factory() as session:
        account = Account(role=AccountRole.USER, status=AccountStatus.SUSPENDED)
        session.add(account)
        await session.flush()
        profile = UserProfile(
            owner_account_id=account.id,
            status=ProfileStatus.ACTIVE,
            name="suspended-owner",
        )
        session.add(profile)
        await session.flush()
        application = Application(
            profile_id=profile.id,
            canonical_job_id=uuid4(),
            source_job_id=uuid4(),
            resume_id=uuid4(),
            recipient_contact_id=uuid4(),
            subject="Test",
            body="Test",
            language="en",
            status=ApplicationStatus.APPROVED,
            policy_result={},
            used_confirmed_facts=[],
            content_validated=True,
            idempotency_key="inactive-owner-" + uuid4().hex,
        )
        session.add(application)
        await session.commit()
        application_id = application.id

    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        email_provider="fake",
        resume_storage_path=tmp_path,
    )
    service = EmailService(settings, sqlite_session_factory)
    with pytest.raises(EmailSendBlocked) as exc_info:
        await service.send_application(application_id)

    assert exc_info.value.reason == "account_or_profile_inactive"
    async with sqlite_session_factory() as session:
        stored = await session.get(Application, application_id)
        assert stored is not None
        assert stored.status == ApplicationStatus.DEFERRED
        assert stored.policy_result["safe_stop_reason"] == "account_or_profile_inactive"

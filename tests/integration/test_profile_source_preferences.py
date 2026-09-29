from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.matching.freshness import build_matching_preselection_query
from app.models.entities import Account, CanonicalJob, JobSource, SourceJob
from app.models.enums import AccountRole, AccountStatus
from app.profiles.schemas import UserProfileInput
from app.profiles.service import ProfileService
from app.profiles.sources import set_source_selected, source_selected
from app.settings import Settings

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_source_choice_is_profile_scoped_and_changes_matching_backlog(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        first_owner = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        second_owner = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add_all([first_owner, second_owner])
        await session.flush()
        profile_service = ProfileService()
        first = await profile_service.create_profile(
            session,
            UserProfileInput(name="First person"),
            owner_account_id=first_owner.id,
        )
        second = await profile_service.create_profile(
            session,
            UserProfileInput(name="Second person"),
            owner_account_id=second_owner.id,
        )
        source = JobSource(
            name="Shared source",
            base_url="https://jobs.example.test",
            adapter_type="fixture_source",
        )
        canonical = CanonicalJob(
            normalized_company="example",
            normalized_title="assistant",
            canonical_fingerprint=uuid4().hex,
        )
        session.add_all([source, canonical])
        await session.flush()
        job = SourceJob(
            source_id=source.id,
            canonical_job_id=canonical.id,
            external_job_id="one",
            canonical_url="https://jobs.example.test/one",
            title="Assistant",
            content_hash="a" * 64,
            matching_content_hash="b" * 64,
            source_fingerprint="c" * 64,
        )
        session.add(job)
        await session.flush()
        settings = Settings(environment="test")

        async def backlog_for(profile_id: UUID) -> int:
            profile = first if profile_id == first.id else second
            preferences = await profile_service.get_preferences(session, profile.id)
            query = build_matching_preselection_query(profile, preferences, [], settings)
            return len((await session.execute(query)).all())

        assert await backlog_for(first.id) == 1
        assert await backlog_for(second.id) == 1
        with pytest.raises(LookupError):
            await set_source_selected(
                session,
                profile_id=second.id,
                source_id=source.id,
                enabled=False,
                owner_account_id=first_owner.id,
            )
        await set_source_selected(
            session,
            profile_id=first.id,
            source_id=source.id,
            enabled=False,
            owner_account_id=first_owner.id,
        )
        assert await source_selected(session, first.id, source.id) is False
        assert await source_selected(session, second.id, source.id) is True
        assert await backlog_for(first.id) == 0
        assert await backlog_for(second.id) == 1
        await set_source_selected(
            session,
            profile_id=first.id,
            source_id=source.id,
            enabled=True,
            owner_account_id=first_owner.id,
        )
        assert await backlog_for(first.id) == 1

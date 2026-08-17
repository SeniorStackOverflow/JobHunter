from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers.parsing.normalization import normalize_for_fingerprint
from app.models.entities import JobPreference, Resume, SourceJob, UserProfile
from app.profiles.schemas import (
    JobPreferenceInput,
    JobPreferenceUpdateInput,
    ResumeMetadataInput,
    UserProfileInput,
)
from app.security.files import safe_storage_path, validate_resume_upload
from app.settings import Settings


def choose_resume_for_job(resumes: list[Resume], job: SourceJob) -> Resume | None:
    normalized_categories = {
        normalize_for_fingerprint(value)
        for value in (job.category, job.subcategory, *(job.categories_seen or []))
        if value
    }
    exact = next(
        (
            resume
            for resume in resumes
            if normalize_for_fingerprint(resume.category) in normalized_categories
        ),
        None,
    )
    return exact or next(
        (resume for resume in resumes if resume.is_default), resumes[0] if resumes else None
    )


class ProfileService:
    async def list_profiles(self, session: AsyncSession) -> list[UserProfile]:
        return list(
            (
                await session.scalars(
                    select(UserProfile).order_by(UserProfile.created_at, UserProfile.id)
                )
            ).all()
        )

    async def get_profile(
        self, session: AsyncSession, profile_id: UUID | None = None
    ) -> UserProfile | None:
        if profile_id is not None:
            return await session.get(UserProfile, profile_id)
        profile = await session.scalar(
            select(UserProfile).where(UserProfile.is_default.is_(True)).limit(1)
        )
        if profile is not None:
            return cast(UserProfile, profile)
        return cast(
            UserProfile | None,
            await session.scalar(
                select(UserProfile).order_by(UserProfile.created_at, UserProfile.id).limit(1)
            ),
        )

    async def create_profile(
        self, session: AsyncSession, payload: UserProfileInput, *, make_default: bool = False
    ) -> UserProfile:
        profiles = await self.list_profiles(session)
        make_default = make_default or not profiles
        if make_default:
            await session.execute(update(UserProfile).values(is_default=False))
        profile = UserProfile(**payload.model_dump(mode="json"), is_default=make_default)
        session.add(profile)
        await session.flush()
        session.add(JobPreference(profile_id=profile.id))
        await session.flush()
        return profile

    async def upsert_profile(
        self, session: AsyncSession, payload: UserProfileInput, profile_id: UUID | None = None
    ) -> UserProfile:
        profile = await self.get_profile(session, profile_id)
        values = payload.model_dump(mode="json")
        if profile is None:
            return await self.create_profile(session, payload, make_default=True)
        for key, value in values.items():
            setattr(profile, key, value)
        await session.flush()
        return profile

    async def set_default_profile(self, session: AsyncSession, profile_id: UUID) -> UserProfile:
        profile = await self.get_profile(session, profile_id)
        if profile is None:
            raise LookupError(f"profile {profile_id} does not exist")
        await session.execute(update(UserProfile).values(is_default=False))
        profile.is_default = True
        await session.flush()
        return profile

    async def get_preferences(
        self, session: AsyncSession, profile_id: UUID | None = None
    ) -> JobPreference:
        profile = await self.get_profile(session, profile_id)
        if profile is None:
            raise LookupError("a user profile is required before preferences")
        preferences = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == profile.id).limit(1)
        )
        if preferences is None:
            preferences = JobPreference(profile_id=profile.id)
            session.add(preferences)
            await session.flush()
        return preferences

    async def upsert_preferences(
        self, session: AsyncSession, payload: JobPreferenceInput, profile_id: UUID | None = None
    ) -> JobPreference:
        preferences = await self.get_preferences(session, profile_id)
        for key, value in payload.model_dump(mode="json").items():
            setattr(preferences, key, value)
        await session.flush()
        return preferences

    async def update_preferences(
        self,
        session: AsyncSession,
        payload: JobPreferenceUpdateInput,
        profile_id: UUID | None = None,
    ) -> JobPreference:
        preferences = await self.get_preferences(session, profile_id)
        for key, value in payload.model_dump(mode="json", exclude_unset=True).items():
            setattr(preferences, key, value)
        await session.flush()
        return preferences

    async def pause_auto_send(
        self, session: AsyncSession, profile_id: UUID | None = None
    ) -> JobPreference:
        preferences = await self.get_preferences(session, profile_id)
        preferences.global_pause = True
        await session.flush()
        return preferences

    async def resume_auto_send(
        self, session: AsyncSession, profile_id: UUID | None = None
    ) -> JobPreference:
        preferences = await self.get_preferences(session, profile_id)
        preferences.auto_send_enabled = True
        preferences.global_pause = False
        await session.flush()
        return preferences


class ResumeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def upload(
        self,
        session: AsyncSession,
        *,
        profile_id: UUID,
        name: str,
        category: str,
        filename: str,
        mime_type: str,
        data: bytes,
        make_default: bool = False,
    ) -> Resume:
        validated = validate_resume_upload(
            filename, mime_type, data, self.settings.max_resume_bytes
        )
        existing = await session.scalar(
            select(Resume).where(Resume.profile_id == profile_id, Resume.sha256 == validated.sha256)
        )
        if existing is not None:
            return existing
        self.settings.resume_storage_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = safe_storage_path(self.settings.resume_storage_path, validated.safe_filename)
        with destination.open("xb") as handle:
            handle.write(validated.data)
        destination.chmod(0o600)
        if make_default:
            await session.execute(
                update(Resume).where(Resume.profile_id == profile_id).values(is_default=False)
            )
        resume = Resume(
            profile_id=profile_id,
            name=name,
            category=category,
            storage_key=validated.safe_filename,
            original_filename=validated.original_filename,
            mime_type=validated.mime_type,
            sha256=validated.sha256,
            active=True,
            verified=False,
            is_default=make_default,
        )
        session.add(resume)
        await session.flush()
        return resume

    async def register_metadata(
        self, session: AsyncSession, payload: ResumeMetadataInput, profile_id: UUID
    ) -> Resume:
        existing = await session.scalar(
            select(Resume).where(
                Resume.profile_id == profile_id, Resume.sha256 == payload.sha256.lower()
            )
        )
        if existing is not None:
            return existing
        resume = Resume(
            profile_id=profile_id,
            name=payload.name,
            category=payload.category,
            storage_key=f"pending/{uuid4().hex}",
            original_filename=Path(payload.original_filename).name,
            mime_type=payload.mime_type,
            sha256=payload.sha256.lower(),
            active=False,
            verified=False,
            is_default=False,
        )
        session.add(resume)
        await session.flush()
        return resume

    async def activate(self, session: AsyncSession, resume_id: UUID) -> Resume:
        resume = await session.get(Resume, resume_id)
        if resume is None:
            raise LookupError(f"resume {resume_id} does not exist")
        path = safe_storage_path(self.settings.resume_storage_path, resume.storage_key)
        if not path.is_file():
            raise ValueError("resume binary has not been uploaded")
        resume.active = True
        await session.flush()
        return resume

    async def deactivate(self, session: AsyncSession, resume_id: UUID) -> Resume:
        resume = await session.get(Resume, resume_id)
        if resume is None:
            raise LookupError(f"resume {resume_id} does not exist")
        resume.active = False
        resume.is_default = False
        await session.flush()
        return resume

    async def select_for_category(
        self, session: AsyncSession, profile_id: UUID, category: str | None
    ) -> Resume | None:
        resumes = list(
            (
                await session.scalars(
                    select(Resume).where(
                        Resume.profile_id == profile_id,
                        Resume.active.is_(True),
                        Resume.verified.is_(True),
                    )
                )
            ).all()
        )
        normalized = (category or "").casefold()
        exact = [item for item in resumes if item.category.casefold() == normalized]
        if exact:
            return exact[0]
        return next((item for item in resumes if item.is_default), resumes[0] if resumes else None)

    async def select_for_job(
        self, session: AsyncSession, profile_id: UUID, job: SourceJob
    ) -> Resume | None:
        resumes = list(
            (
                await session.scalars(
                    select(Resume).where(
                        Resume.profile_id == profile_id,
                        Resume.active.is_(True),
                        Resume.verified.is_(True),
                    )
                )
            ).all()
        )
        return choose_resume_for_job(resumes, job)

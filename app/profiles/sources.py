from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.entities import JobSource, ProfileSourcePreference, UserProfile


def source_selected_clause(profile_id: UUID, source_id: Any) -> ColumnElement[bool]:
    """Missing preference rows preserve the existing enabled-for-everyone behavior."""

    return ~exists(
        select(ProfileSourcePreference.profile_id).where(
            ProfileSourcePreference.profile_id == profile_id,
            ProfileSourcePreference.source_id == source_id,
            ProfileSourcePreference.enabled.is_(False),
        )
    )


async def source_selected(session: AsyncSession, profile_id: UUID, source_id: UUID) -> bool:
    enabled = await session.scalar(
        select(ProfileSourcePreference.enabled).where(
            ProfileSourcePreference.profile_id == profile_id,
            ProfileSourcePreference.source_id == source_id,
        )
    )
    return enabled is not False


async def set_source_selected(
    session: AsyncSession,
    *,
    profile_id: UUID,
    source_id: UUID,
    enabled: bool,
    owner_account_id: UUID | None = None,
) -> ProfileSourcePreference:
    profile_query = select(UserProfile).where(UserProfile.id == profile_id).with_for_update()
    if owner_account_id is not None:
        profile_query = profile_query.where(UserProfile.owner_account_id == owner_account_id)
    profile = await session.scalar(profile_query)
    if profile is None or await session.get(JobSource, source_id) is None:
        raise LookupError("profile or source not found")
    preference = await session.get(ProfileSourcePreference, (profile_id, source_id))
    if preference is None:
        preference = ProfileSourcePreference(
            profile_id=profile_id, source_id=source_id, enabled=enabled
        )
        session.add(preference)
    else:
        preference.enabled = enabled
    await session.flush()
    return preference


__all__ = ["set_source_selected", "source_selected", "source_selected_clause"]

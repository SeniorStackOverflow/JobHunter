"""Per-source category choices of a profile.

Each adapter publishes its own categories, so the choice is stored per
(profile, source) and validated against the catalogue the crawler discovered
for that source. The profile-wide lists on ``JobPreference`` remain as a mirror
(the union over all configured sources) for code that hashes or exposes them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    JobPreference,
    JobSource,
    ProfileSourcePreference,
    SourceCategory,
    UserProfile,
)

_PREFERRED_LOCALES = ("ru", "ro", "en")


@dataclass(frozen=True)
class SourceCategoryPolicy:
    search: tuple[str, ...]
    auto_send: tuple[str, ...]
    excluded: tuple[str, ...]
    # False: the source still follows the profile-wide lists.
    configured: bool


@dataclass(frozen=True)
class SourceCategoryOption:
    external_id: str
    name: str


def _ordered(values: Iterable[str]) -> list[str]:
    return sorted({value.strip() for value in values if isinstance(value, str) and value.strip()})


async def category_policy(
    session: AsyncSession, preference: JobPreference, source_id: UUID | None
) -> SourceCategoryPolicy:
    row = (
        await session.get(ProfileSourcePreference, (preference.profile_id, source_id))
        if source_id is not None
        else None
    )
    if row is None or not row.categories_configured:
        return SourceCategoryPolicy(
            search=tuple(preference.allowed_categories or []),
            auto_send=tuple(preference.auto_send_categories or []),
            excluded=tuple(preference.forbidden_categories or []),
            configured=False,
        )
    return SourceCategoryPolicy(
        search=tuple(row.search_categories or []),
        auto_send=tuple(row.auto_send_categories or []),
        excluded=tuple(row.excluded_categories or []),
        configured=True,
    )


async def known_source_categories(
    session: AsyncSession, source_id: UUID
) -> list[SourceCategoryOption]:
    """Active categories the source publishes, one name per category."""

    rows = (
        await session.execute(
            select(SourceCategory.external_id, SourceCategory.name, SourceCategory.locale).where(
                SourceCategory.source_id == source_id, SourceCategory.active.is_(True)
            )
        )
    ).all()
    names: dict[str, tuple[int, str]] = {}
    for external_id, name, locale in rows:
        rank = (
            _PREFERRED_LOCALES.index(locale)
            if locale in _PREFERRED_LOCALES
            else len(_PREFERRED_LOCALES)
        )
        if external_id not in names or rank < names[external_id][0]:
            names[external_id] = (rank, name)
    return sorted(
        (SourceCategoryOption(external_id, name) for external_id, (_rank, name) in names.items()),
        key=lambda item: (item.name.casefold(), item.external_id),
    )


async def _mirror_profile_lists(session: AsyncSession, profile_id: UUID) -> None:
    preference = await session.scalar(
        select(JobPreference).where(JobPreference.profile_id == profile_id)
    )
    if preference is None:
        return
    rows = (
        await session.scalars(
            select(ProfileSourcePreference).where(
                ProfileSourcePreference.profile_id == profile_id,
                ProfileSourcePreference.categories_configured.is_(True),
            )
        )
    ).all()
    if not rows:
        return
    preference.allowed_categories = _ordered(
        value for row in rows for value in row.search_categories or []
    )
    preference.auto_send_categories = _ordered(
        value for row in rows for value in row.auto_send_categories or []
    )
    preference.forbidden_categories = _ordered(
        value for row in rows for value in row.excluded_categories or []
    )


async def set_source_categories(
    session: AsyncSession,
    *,
    profile_id: UUID,
    source_id: UUID,
    search: Iterable[str],
    auto_send: Iterable[str],
    excluded: Iterable[str],
    owner_account_id: UUID | None = None,
) -> ProfileSourcePreference:
    profile_query = select(UserProfile).where(UserProfile.id == profile_id).with_for_update()
    if owner_account_id is not None:
        profile_query = profile_query.where(UserProfile.owner_account_id == owner_account_id)
    profile = await session.scalar(profile_query)
    if profile is None or await session.get(JobSource, source_id) is None:
        raise LookupError("profile or source not found")

    known = {
        external_id
        for external_id in (
            await session.scalars(
                select(SourceCategory.external_id).where(SourceCategory.source_id == source_id)
            )
        ).all()
    }
    excluded_ids = _ordered(excluded)
    auto_ids = [item for item in _ordered(auto_send) if item not in excluded_ids]
    # Automatic sending is a stronger choice than searching; exclusion beats both.
    search_ids = [item for item in _ordered([*search, *auto_ids]) if item not in excluded_ids]
    for external_id in (*search_ids, *excluded_ids):
        if external_id not in known:
            raise ValueError(f"unknown source category: {external_id}")

    row = await session.get(ProfileSourcePreference, (profile_id, source_id))
    if row is None:
        row = ProfileSourcePreference(profile_id=profile_id, source_id=source_id, enabled=True)
        session.add(row)
    row.categories_configured = True
    row.search_categories = search_ids
    row.auto_send_categories = auto_ids
    row.excluded_categories = excluded_ids
    await session.flush()
    await _mirror_profile_lists(session, profile_id)
    await session.flush()
    return row


async def crawl_category_slugs(session: AsyncSession, source_id: UUID) -> list[str]:
    """Categories that processed profiles search on this source.

    Empty means nobody has chosen categories for the source yet; the caller keeps
    the source's own configured default.
    """
    from app.profiles.service import ProfileService

    profile_ids = [
        profile.id for profile in await ProfileService().list_processing_profiles(session)
    ]
    if not profile_ids:
        return []
    rows = (
        await session.scalars(
            select(ProfileSourcePreference).where(
                ProfileSourcePreference.source_id == source_id,
                ProfileSourcePreference.profile_id.in_(profile_ids),
                ProfileSourcePreference.enabled.is_(True),
                ProfileSourcePreference.categories_configured.is_(True),
            )
        )
    ).all()
    return _ordered(value for row in rows for value in row.search_categories or [])


__all__ = [
    "SourceCategoryOption",
    "SourceCategoryPolicy",
    "category_policy",
    "crawl_category_slugs",
    "known_source_categories",
    "set_source_categories",
]

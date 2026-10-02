"""Per-source category choices of a profile.

Each adapter publishes its own categories, so the choice is stored per
(profile, source) and validated against the catalogue the crawler discovered
for that source. The profile-wide lists on ``JobPreference`` stay as the default
for a source that has no choice of its own; they are part of the evaluation
fingerprint and are never rewritten here, so a per-source change re-evaluates
only the vacancies it affects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    JobPreference,
    JobSource,
    MatchEvaluation,
    ProfileSourcePreference,
    SourceCategory,
    SourceJob,
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


CATEGORY_STATES = ("off", "search", "auto", "excluded")
_STATE_ORDER = {"auto": 0, "search": 1, "excluded": 2, "off": 3}
_FORM_PREFIX = "category:"


@dataclass(frozen=True)
class SourceCategoryChoice:
    external_id: str
    name: str
    state: str


@dataclass(frozen=True)
class SourceCategoryPicker:
    choices: list[SourceCategoryChoice]
    searched: int
    auto_sent: int
    excluded: int
    # Stored values the source does not publish (typed by hand before the picker).
    unknown: list[str]
    configured: bool


async def source_category_picker(
    session: AsyncSession, preference: JobPreference, source_id: UUID
) -> SourceCategoryPicker:
    """Every category the source publishes with the profile's choice for it."""

    policy = await category_policy(session, preference, source_id)
    options = await known_source_categories(session, source_id)
    known = {option.external_id for option in options}

    def state(external_id: str) -> str:
        if external_id in policy.excluded:
            return "excluded"
        if external_id in policy.auto_send:
            return "auto"
        if external_id in policy.search:
            return "search"
        return "off"

    choices = sorted(
        (
            SourceCategoryChoice(option.external_id, option.name, state(option.external_id))
            for option in options
        ),
        key=lambda item: (_STATE_ORDER[item.state], item.name.casefold(), item.external_id),
    )
    return SourceCategoryPicker(
        choices=choices,
        searched=sum(item.state in {"search", "auto"} for item in choices),
        auto_sent=sum(item.state == "auto" for item in choices),
        excluded=sum(item.state == "excluded" for item in choices),
        unknown=_ordered(
            value
            for value in (*policy.search, *policy.auto_send, *policy.excluded)
            if value not in known
        ),
        configured=policy.configured,
    )


def category_lists_from_states(
    form: Mapping[str, object],
) -> tuple[list[str], list[str], list[str]]:
    """Turn the picker form (``category:<id>`` → state) into search/auto/excluded."""

    search: list[str] = []
    auto_send: list[str] = []
    excluded: list[str] = []
    for key, value in form.items():
        if not key.startswith(_FORM_PREFIX):
            continue
        external_id = key.removeprefix(_FORM_PREFIX)
        if value not in CATEGORY_STATES:
            raise ValueError(f"unknown category state: {value!r}")
        if value == "search":
            search.append(external_id)
        elif value == "auto":
            auto_send.append(external_id)
        elif value == "excluded":
            excluded.append(external_id)
    return _ordered(search), _ordered(auto_send), _ordered(excluded)


async def _mark_changed_vacancies_stale(
    session: AsyncSession,
    *,
    profile_id: UUID,
    source_id: UUID,
    before: SourceCategoryPolicy,
    after: SourceCategoryPolicy,
) -> int:
    """Mark the profile's evaluations of vacancies whose category verdict changed.

    Only those vacancies need the prefilter and the model again. An unset
    preference fingerprint is the matcher's existing "not current" state.
    Auto-send is not model input, so it never makes an evaluation stale.
    """
    from app.matching.prefilter import category_verdict

    if (before.search, before.excluded) == (after.search, after.excluded):
        return 0
    jobs = (
        await session.execute(
            select(
                SourceJob.id, SourceJob.category, SourceJob.subcategory, SourceJob.categories_seen
            ).where(SourceJob.source_id == source_id)
        )
    ).all()
    changed: list[UUID] = []
    for job_id, category, subcategory, categories_seen in jobs:
        categories = [item for item in (category, subcategory, *(categories_seen or [])) if item]
        if category_verdict(categories, before.search, before.excluded) != category_verdict(
            categories, after.search, after.excluded
        ):
            changed.append(job_id)
    marked = 0
    for start in range(0, len(changed), 500):
        result = await session.execute(
            update(MatchEvaluation)
            .where(
                MatchEvaluation.profile_id == profile_id,
                MatchEvaluation.source_job_id.in_(changed[start : start + 500]),
                MatchEvaluation.preference_fingerprint.is_not(None),
            )
            .values(preference_fingerprint=None)
            .execution_options(synchronize_session="fetch")
        )
        marked += int(getattr(result, "rowcount", 0) or 0)
    return marked


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

    preference = await session.scalar(
        select(JobPreference).where(JobPreference.profile_id == profile_id)
    )
    before = (
        await category_policy(session, preference, source_id) if preference is not None else None
    )
    row = await session.get(ProfileSourcePreference, (profile_id, source_id))
    if row is None:
        row = ProfileSourcePreference(profile_id=profile_id, source_id=source_id, enabled=True)
        session.add(row)
    row.categories_configured = True
    row.search_categories = search_ids
    row.auto_send_categories = auto_ids
    row.excluded_categories = excluded_ids
    await session.flush()
    if before is not None:
        await _mark_changed_vacancies_stale(
            session,
            profile_id=profile_id,
            source_id=source_id,
            before=before,
            after=SourceCategoryPolicy(
                search=tuple(search_ids),
                auto_send=tuple(auto_ids),
                excluded=tuple(excluded_ids),
                configured=True,
            ),
        )
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
    "CATEGORY_STATES",
    "SourceCategoryChoice",
    "SourceCategoryOption",
    "SourceCategoryPicker",
    "SourceCategoryPolicy",
    "category_lists_from_states",
    "category_policy",
    "crawl_category_slugs",
    "known_source_categories",
    "set_source_categories",
    "source_category_picker",
]

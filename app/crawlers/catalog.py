from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import JobSource
from app.models.enums import SourceHealth

if TYPE_CHECKING:
    from app.crawlers.registry import JobSourceAdapterRegistry


@dataclass(frozen=True)
class SourceDefinition:
    """A built-in site's stable identity and defaults for its first registration."""

    key: str
    name: str
    base_url: str
    configuration: dict[str, Any] = field(default_factory=dict)
    rate_limit: int = 20
    concurrency: int = 1
    requires_policy_review: bool = False

    def __post_init__(self) -> None:
        if not self.key.strip() or len(self.key) > 100:
            raise ValueError("source catalog key must be non-empty and at most 100 characters")


async def reconcile_source_catalog(
    session: AsyncSession, registry: JobSourceAdapterRegistry
) -> list[str]:
    """Register missing sites; adopt legacy rows without replacing operator choices."""
    if session.get_bind().dialect.name == "postgresql":
        # Multiple API processes may start together; the lock lasts until commit.
        await session.execute(text("SELECT pg_advisory_xact_lock(865321047)"))
    existing = list(
        (
            await session.scalars(select(JobSource).order_by(JobSource.created_at, JobSource.id))
        ).all()
    )
    created = []
    for adapter_type, definition in registry.source_definitions().items():
        if any(item.catalog_key == definition.key for item in existing):
            continue
        legacy = next(
            (
                item
                for item in existing
                if item.catalog_key is None
                and item.adapter_type == adapter_type
                and item.base_url.rstrip("/") == definition.base_url.rstrip("/")
            ),
            None,
        )
        if legacy is not None:
            legacy.catalog_key = definition.key
            continue
        source = JobSource(
            catalog_key=definition.key,
            name=definition.name,
            base_url=definition.base_url,
            adapter_type=adapter_type,
            configuration=deepcopy(definition.configuration),
            rate_limit=definition.rate_limit,
            concurrency=definition.concurrency,
            enabled=False,
            health_status=SourceHealth.PAUSED,
            automatic_actions_paused=True,
        )
        session.add(source)
        existing.append(source)
        created.append(definition.key)
    await session.flush()
    return created

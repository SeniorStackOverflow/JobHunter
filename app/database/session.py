from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import Settings, get_settings


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    current = settings or get_settings()
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if current.database_url.startswith("sqlite"):
        kwargs["pool_pre_ping"] = False
    return create_async_engine(current.database_url, **kwargs)


engine = build_engine()
async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session


def make_session_factory(engine_: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine_, expire_on_commit=False, class_=AsyncSession)

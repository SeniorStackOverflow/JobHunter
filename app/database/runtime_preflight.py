from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


class RuntimePreflightError(RuntimeError):
    """Raised when a production container does not match its deployment contract."""


def validate_build_provenance(
    *,
    actual_flavor: str | None,
    expected_flavor: str | None,
    actual_revision: str | None,
    expected_revision: str | None,
) -> None:
    if expected_flavor and actual_flavor != expected_flavor:
        raise RuntimePreflightError(
            f"build flavor mismatch: expected {expected_flavor!r}, got {actual_flavor!r}"
        )
    if expected_revision and actual_revision != expected_revision:
        raise RuntimePreflightError(
            f"image revision mismatch: expected {expected_revision!r}, got {actual_revision!r}"
        )


async def current_database_user(database_url: str) -> str:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            return str(await connection.scalar(text("select current_user")))
    finally:
        await engine.dispose()


async def run_preflight() -> None:
    validate_build_provenance(
        actual_flavor=os.getenv("APP_BUILD_FLAVOR"),
        expected_flavor=os.getenv("EXPECTED_BUILD_FLAVOR"),
        actual_revision=os.getenv("APP_REVISION"),
        expected_revision=os.getenv("EXPECTED_APP_REVISION"),
    )
    expected_role = os.getenv("EXPECTED_DATABASE_ROLE")
    if expected_role:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimePreflightError(
                "DATABASE_URL is required when EXPECTED_DATABASE_ROLE is configured"
            )
        actual_role = await current_database_user(database_url)
        if actual_role != expected_role:
            raise RuntimePreflightError(
                f"database role mismatch: expected {expected_role!r}, got {actual_role!r}"
            )


def main() -> None:
    try:
        asyncio.run(run_preflight())
    except RuntimePreflightError as exc:
        print(f"jobhunter runtime preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(78) from exc


if __name__ == "__main__":
    main()

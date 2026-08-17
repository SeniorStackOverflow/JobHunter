from __future__ import annotations

import asyncio
from typing import Any, cast

from fastapi import APIRouter
from redis.asyncio import Redis
from sqlalchemy import text
from starlette.responses import JSONResponse, Response

from app.database import async_session_factory
from app.observability.logging import safe_exception_name
from app.observability.metrics import metrics_response
from app.settings import get_settings

router = APIRouter(tags=["observability"])


async def _database_health() -> tuple[bool, str | None]:
    try:
        async with async_session_factory() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=3.0)
    except Exception as exc:
        return False, safe_exception_name(exc)
    return True, None


async def _redis_health() -> tuple[bool, str | None]:
    client: Redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        await asyncio.wait_for(client.ping(), timeout=3.0)
    except Exception as exc:
        return False, safe_exception_name(exc)
    finally:
        await client.aclose()
    return True, None


async def readiness_status() -> tuple[bool, dict[str, dict[str, Any]]]:
    database, redis = await asyncio.gather(_database_health(), _redis_health())
    checks = {
        "database": {"ok": database[0], "error_type": database[1]},
        "redis": {"ok": redis[0], "error_type": redis[1]},
    }
    return all(check["ok"] is True for check in checks.values()), checks


async def celery_health_status() -> tuple[bool, int]:
    def inspect_workers() -> dict[str, Any] | None:
        from app.scheduler.celery_app import celery_app

        return cast(dict[str, Any] | None, celery_app.control.inspect(timeout=2.0).ping())

    try:
        replies = await asyncio.wait_for(asyncio.to_thread(inspect_workers), timeout=3.0)
    except Exception:
        return False, 0
    return bool(replies), len(replies or {})


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", include_in_schema=False)
async def ready() -> JSONResponse:
    is_ready, checks = await readiness_status()
    return JSONResponse(
        {"status": "ready" if is_ready else "not_ready", "checks": checks},
        status_code=200 if is_ready else 503,
    )


@router.get("/health/celery", include_in_schema=False)
async def celery_health() -> JSONResponse:
    healthy, workers = await celery_health_status()
    return JSONResponse(
        {"status": "ok" if healthy else "unavailable", "workers": workers},
        status_code=200 if healthy else 503,
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return metrics_response()


__all__ = ["celery_health_status", "readiness_status", "router"]

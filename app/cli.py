from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from app.database.session import async_session_factory
from app.models.entities import JobSource
from app.models.enums import SourceHealth
from app.profiles import ProfileService
from app.profiles.schemas import UserProfileInput
from app.security.auth import hash_api_key, hash_password


async def seed_defaults(include_fixture: bool) -> None:
    async with async_session_factory() as session:
        profile_service = ProfileService()
        profile = await profile_service.get_profile(session)
        if profile is None:
            profile = await profile_service.create_profile(
                session,
                UserProfileInput(name="Основной профиль"),
                make_default=True,
            )
        await profile_service.get_preferences(session, profile.id)
        rabota = await session.scalar(
            select(JobSource).where(JobSource.adapter_type == "rabota_md")
        )
        if rabota is None:
            session.add(
                JobSource(
                    name="Rabota.md",
                    base_url="https://www.rabota.md",
                    adapter_type="rabota_md",
                    configuration={
                        "locale_priority": ["ru"],
                        "use_stealth_browser": True,
                        "requests_per_minute": 50,
                        "minimum_interval_seconds": 1.2,
                        "policy_review_acknowledged": True,
                        "policy_review_reference": "operator-approved-2026-08-11",
                        "incremental_scan": {
                            "schedule": "0 * * * *",
                            "category_slugs": ["others"],
                            "known_unchanged_stop_threshold": 100,
                            "known_detail_refresh_hours": 24,
                            "max_pages_per_entrypoint": 20,
                        },
                        "active_job_recheck": {
                            "schedule": "0 2 * * *",
                            "close_after_confirmed_absence_count": 3,
                        },
                        "full_scan": {
                            "schedule": "0 3 * * *",
                            "resume_from_checkpoint": True,
                        },
                    },
                    enabled=False,
                    rate_limit=50,
                    concurrency=1,
                    health_status=SourceHealth.PAUSED,
                    automatic_actions_paused=True,
                )
            )
        if include_fixture:
            fixture = await session.scalar(
                select(JobSource).where(JobSource.adapter_type == "fixture_source")
            )
            if fixture is None:
                session.add(
                    JobSource(
                        name="Local Fixture Jobs",
                        base_url="http://fixture-site:8090",
                        adapter_type="fixture_source",
                        configuration={"allowed_domains": ["fixture-site"]},
                        enabled=True,
                        rate_limit=600,
                        concurrency=2,
                        health_status=SourceHealth.UNKNOWN,
                    )
                )
        await session.commit()


def validate_source_config(path: Path) -> dict[str, Any]:
    from app.crawlers.adapters.rabota_md import RabotaMdConfig
    from app.crawlers.adapters.structured import StructuredSourceConfig
    from app.crawlers.schemas import GenericSourceConfig

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    source = raw["source"]
    adapter_type = str(source.get("adapter", "")).casefold()
    parsed: GenericSourceConfig | RabotaMdConfig | StructuredSourceConfig
    if adapter_type in {"generic_html", "company_careers", "fixture_source"}:
        parsed = GenericSourceConfig.model_validate(source)
    elif adapter_type == "rabota_md":
        incremental = source.get("incremental_scan", {})
        values = {
            "base_url": source.get("base_url"),
            "live_mode": source.get("live_mode", True),
            "policy_review_acknowledged": source.get("policy_review_acknowledged", False),
            "policy_review_reference": source.get("policy_review_reference"),
            "locale_priority": source.get("locale_priority", ["ru"]),
            "use_stealth_browser": source.get("use_stealth_browser", True),
            "requests_per_minute": source.get("requests_per_minute", 50),
            "minimum_interval_seconds": source.get("minimum_interval_seconds", 1.2),
            "incremental_max_pages_per_entrypoint": incremental.get("max_pages_per_entrypoint", 20),
            "known_unchanged_stop_threshold": incremental.get(
                "known_unchanged_stop_threshold", 100
            ),
            "incremental_category_slugs": incremental.get("category_slugs", ["others"]),
            "incremental_known_detail_refresh_hours": incremental.get(
                "known_detail_refresh_hours", 24
            ),
        }
        parsed = RabotaMdConfig.model_validate(values)
    elif adapter_type in {"generic_api", "rss", "sitemap"}:
        parsed = StructuredSourceConfig.model_validate(source)
    else:
        raise ValueError(f"unsupported adapter type {adapter_type!r}")
    return parsed.model_dump(mode="json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    password = subparsers.add_parser("hash-password", help="hash an admin password")
    password.add_argument(
        "password",
        nargs="?",
        help="omit to read the password without echo (preferred)",
    )
    api_key = subparsers.add_parser("hash-api-key", help="hash an MCP/API bearer key")
    api_key.add_argument(
        "api_key",
        nargs="?",
        help="omit to read the key without echo (preferred)",
    )
    seed = subparsers.add_parser("seed", help="create safe default source and policy rows")
    seed.add_argument("--include-fixture", action="store_true")
    config = subparsers.add_parser("validate-source-config")
    config.add_argument("path", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "hash-password":
        value = args.password or getpass.getpass("Admin password: ")
        if not value:
            raise SystemExit("password cannot be empty")
        print(hash_password(value))
    elif args.command == "hash-api-key":
        value = args.api_key or getpass.getpass("MCP/API bearer key: ")
        if not value:
            raise SystemExit("API key cannot be empty")
        print(hash_api_key(value))
    elif args.command == "seed":
        asyncio.run(seed_defaults(args.include_fixture))
    elif args.command == "validate-source-config":
        print(json.dumps(validate_source_config(args.path), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

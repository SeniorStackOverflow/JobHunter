from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import select

from app.database.session import async_session_factory
from app.email.delivery import EmailDeliveryReconciliationService
from app.employers import EmployerBackfillService, EmployerSafetyAuditService
from app.models.entities import JobSource
from app.models.enums import SourceHealth
from app.profiles import ProfileService
from app.profiles.schemas import UserProfileInput
from app.security.auth import hash_api_key, hash_password
from app.settings import get_settings


async def employer_identity_preview(company: str | None) -> dict[str, Any]:
    async with async_session_factory() as session:
        return await EmployerBackfillService().preview_identity(session, company_filter=company)


async def minimum_audit(profile_id: UUID, day: date | None) -> dict[str, Any]:
    from app.applications.diagnostics import daily_minimum_audit

    async with async_session_factory() as session:
        with session.no_autoflush:
            result = await daily_minimum_audit(session, profile_id, day=day)
        await session.rollback()
        return result


async def employer_relationship_audit(company: str | None) -> dict[str, Any]:
    async with async_session_factory() as session:
        return await EmployerSafetyAuditService().report(session, company_filter=company)


async def apply_employer_backfill() -> dict[str, int]:
    async with async_session_factory() as session:
        result = await EmployerBackfillService().apply(session)
        await session.commit()
        return result


async def apply_employer_remediation() -> dict[str, int]:
    async with async_session_factory() as session:
        result = await EmployerSafetyAuditService().remediate_unsent(session)
        await session.commit()
        return result


async def record_employer_historical_incidents() -> dict[str, int]:
    async with async_session_factory() as session:
        result = await EmployerSafetyAuditService().record_historical_incidents(session)
        await session.commit()
        return result


async def email_delivery_audit(recipient: str | None) -> dict[str, object]:
    return await EmailDeliveryReconciliationService(
        get_settings(), async_session_factory
    ).audit_mailbox(recipient_filter=recipient)


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
                            "known_detail_refresh_hours": 72,
                            "refresh_jitter_hours": 12,
                            "detail_refresh_budget": 50,
                            "max_pages_per_entrypoint": 20,
                        },
                        "active_job_recheck": {
                            "schedule": "20 * * * *",
                            "close_after_confirmed_absence_count": 3,
                            "max_jobs_per_run": 300,
                            "min_recheck_interval_hours": 20,
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
        delucru = await session.scalar(
            select(JobSource).where(JobSource.adapter_type == "delucru_md")
        )
        if delucru is None:
            session.add(
                JobSource(
                    name="Delucru.md",
                    base_url="https://www.delucru.md",
                    adapter_type="delucru_md",
                    configuration={
                        "live_mode": True,
                        "policy_review_acknowledged": False,
                        "locale_priority": ["ro", "ru"],
                        "requests_per_minute": 25,
                        "minimum_interval_seconds": 2.0,
                    },
                    enabled=False,
                    rate_limit=25,
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
    from app.crawlers.adapters.delucru_md import DelucruMdConfig
    from app.crawlers.adapters.rabota_md import RabotaMdConfig
    from app.crawlers.adapters.structured import StructuredSourceConfig
    from app.crawlers.schemas import GenericSourceConfig

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    source = raw["source"]
    adapter_type = str(source.get("adapter", "")).casefold()
    parsed: GenericSourceConfig | RabotaMdConfig | DelucruMdConfig | StructuredSourceConfig
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
                "known_detail_refresh_hours", 72
            ),
            "incremental_refresh_jitter_hours": incremental.get("refresh_jitter_hours", 12),
            "incremental_detail_refresh_budget": incremental.get("detail_refresh_budget", 50),
        }
        parsed = RabotaMdConfig.model_validate(values)
    elif adapter_type == "delucru_md":
        from app.crawlers.adapters.delucru_md import DelucruMdConfig

        incremental = source.get("incremental_scan", {})
        values = {
            "base_url": source.get("base_url", "https://www.delucru.md"),
            "live_mode": source.get("live_mode", True),
            "policy_review_acknowledged": source.get("policy_review_acknowledged", False),
            "policy_review_reference": source.get("policy_review_reference"),
            "locale_priority": source.get("locale_priority", ["ro", "ru"]),
            "requests_per_minute": source.get("requests_per_minute", 25),
            "minimum_interval_seconds": source.get("minimum_interval_seconds", 2.0),
            "incremental_max_pages_per_entrypoint": incremental.get("max_pages_per_entrypoint", 10),
            "known_unchanged_stop_threshold": incremental.get("known_unchanged_stop_threshold", 50),
            "incremental_category_slugs": incremental.get(
                "category_slugs", ["it-internet", "lucru-de-acasa-part-time"]
            ),
        }
        parsed = DelucruMdConfig.model_validate(values)
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
    subparsers.add_parser(
        "phone-agent",
        help="run the read-only PhoneGate call observer",
    )
    identity_audit = subparsers.add_parser(
        "employer-identity-audit", help="preview canonical-employer identity evidence"
    )
    identity_audit.add_argument("--company")
    relationship_audit = subparsers.add_parser(
        "employer-relationship-audit", help="run the read-only A-E safety audit"
    )
    relationship_audit.add_argument("--company")
    employer_backfill = subparsers.add_parser(
        "employer-backfill", help="reconstruct canonical employers and relationship events"
    )
    employer_backfill.add_argument("--apply", action="store_true", required=True)
    employer_remediation = subparsers.add_parser(
        "employer-remediate", help="cancel/defer unsafe unsent applications"
    )
    employer_remediation.add_argument("--apply", action="store_true", required=True)
    historical_incidents = subparsers.add_parser(
        "employer-historical-incidents",
        help="record audited historical send incidents without changing applications",
    )
    historical_incidents.add_argument("--apply", action="store_true", required=True)
    delivery_audit = subparsers.add_parser(
        "email-delivery-audit", help="read recent Gmail DSNs without mutating state"
    )
    delivery_audit.add_argument("--recipient")
    minimum = subparsers.add_parser(
        "daily-minimum-audit", help="read-only distinct-employer replay"
    )
    minimum.add_argument("--profile-id", type=UUID, required=True)
    minimum.add_argument("--day", type=date.fromisoformat)
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
    elif args.command == "daily-minimum-audit":
        print(
            json.dumps(
                asyncio.run(minimum_audit(args.profile_id, args.day)), indent=2, ensure_ascii=False
            )
        )
    elif args.command == "validate-source-config":
        print(json.dumps(validate_source_config(args.path), indent=2, ensure_ascii=False))
    elif args.command == "phone-agent":
        from app.phone.agent import main as phone_agent_main

        phone_agent_main()
    elif args.command == "employer-identity-audit":
        print(
            json.dumps(
                asyncio.run(employer_identity_preview(args.company)),
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.command == "employer-relationship-audit":
        print(
            json.dumps(
                asyncio.run(employer_relationship_audit(args.company)),
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.command == "employer-backfill":
        print(json.dumps(asyncio.run(apply_employer_backfill()), indent=2))
    elif args.command == "employer-remediate":
        print(json.dumps(asyncio.run(apply_employer_remediation()), indent=2))
    elif args.command == "employer-historical-incidents":
        print(json.dumps(asyncio.run(record_employer_historical_incidents()), indent=2))
    elif args.command == "email-delivery-audit":
        print(
            json.dumps(
                asyncio.run(email_delivery_audit(args.recipient)),
                indent=2,
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()

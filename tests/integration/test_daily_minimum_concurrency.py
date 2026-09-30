from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.applications.daily_target import daily_target_state
from app.database.base import Base
from app.models.entities import Application
from app.models.enums import ApplicationStatus
from tests.unit.test_daily_minimum import additional_candidate
from tests.unit.test_policy_and_email import make_graph, settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("MINIMUM_TEST_DATABASE_URL"),
        reason="requires an isolated PostgreSQL test DB",
    ),
]


@pytest.mark.parametrize("same_company", [False, True])
async def test_parallel_promotions_reserve_only_missing_distinct_companies(tmp_path, same_company):
    database_url = os.environ["MINIMUM_TEST_DATABASE_URL"]
    schema_name = f"minimum_test_{uuid4().hex}"
    control = create_async_engine(database_url)
    engine = create_async_engine(
        database_url, connect_args={"server_settings": {"search_path": schema_name}}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with control.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            graph = await make_graph(session, tmp_path)
            preference = graph[2]
            preference.minimum_auto_send_score = 70
            preference.additional_rules = {"minimum_daily_applications": 2 if same_company else 1}
            graph[6].overall_fit = 45
            from app.models.enums import MatchDecision

            graph[6].decision = MatchDecision.PREPARE_FOR_REVIEW
            second = await additional_candidate(
                session,
                graph,
                score=45,
                company="Example Company" if same_company else "Second Company",
            )
            identifiers = [graph[-1].id, second[-1].id]
            preference_id = preference.id
            await session.commit()

        async def promote(identifier):
            from app.applications.service import ApplicationService

            async with factory() as session:
                application = await session.scalar(
                    select(Application).where(Application.id == identifier).with_for_update()
                )
                await ApplicationService(settings(tmp_path)).reevaluate_policy(session, application)
                await session.commit()

        await asyncio.wait_for(
            asyncio.gather(*(promote(identifier) for identifier in identifiers)), timeout=20
        )
        async with factory() as session:
            applications = list((await session.scalars(select(Application))).all())
            assert (
                sum(
                    application.status is ApplicationStatus.AUTO_APPROVED
                    for application in applications
                )
                == 1
            )
            preference = await session.get(type(preference), preference_id)
            target = await daily_target_state(session, preference)
            assert target.reserved == 1
            assert target.remaining == (1 if same_company else 0)
    finally:
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await control.dispose()


async def test_parallel_senders_never_exceed_hard_maximum(tmp_path):
    from app.delivery_ledger import transmissions_on_day
    from app.email.providers import FakeGmailProvider
    from app.email.service import EmailSendBlocked, EmailService
    from app.models.entities import EmailDelivery, EmailSendAttempt
    from app.models.enums import DeliveryStatus, MatchDecision, PolicyDecision

    database_url = os.environ["MINIMUM_TEST_DATABASE_URL"]
    schema_name = f"maximum_test_{uuid4().hex}"
    control = create_async_engine(database_url)
    engine = create_async_engine(
        database_url, connect_args={"server_settings": {"search_path": schema_name}}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with control.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            graph = await make_graph(session, tmp_path)
            preference = graph[2]
            preference.maximum_daily_applications = 1
            second = await additional_candidate(
                session,
                graph,
                score=92,
                company="Second Company",
                decision=MatchDecision.AUTO_APPLY,
            )
            applications = [graph[-1], second[-1]]
            for application in applications:
                application.status = ApplicationStatus.AUTO_APPROVED
                application.policy_decision = PolicyDecision.AUTO_APPROVED
            identifiers = [application.id for application in applications]
            profile_id = graph[1].id
            await session.commit()

        provider = FakeGmailProvider()

        async def send(identifier):
            service = EmailService(settings(tmp_path), factory, provider)
            try:
                return await service.send_application(identifier)
            except EmailSendBlocked as exc:
                return exc

        results = await asyncio.wait_for(
            asyncio.gather(*(send(identifier) for identifier in identifiers)), timeout=30
        )
        accepted = [
            item
            for item in results
            if isinstance(item, EmailDelivery) and item.status is DeliveryStatus.PROVIDER_ACCEPTED
        ]
        assert len(accepted) == 1
        assert len(provider.outbox) == 1
        async with factory() as session:
            assert await transmissions_on_day(session, profile_id=profile_id) == 1
            assert len((await session.scalars(select(EmailSendAttempt))).all()) == 1
    finally:
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await control.dispose()

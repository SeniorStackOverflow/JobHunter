from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.employers import (
    EmployerBackfillService,
    EmployerIdentityService,
    EmployerRelationshipService,
    EmployerSafetyAuditService,
    classify_candidate_decline,
)
from app.models.entities import (
    Application,
    ApplicationPolicyRefreshQueue,
    AuditEvent,
    CanonicalEmployer,
    CanonicalJob,
    EmailDelivery,
    EmployerIdentityCandidate,
    EmployerInteractionEvent,
    JobSource,
    MatchEvaluation,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    DeliveryStatus,
    EmployerIdentifierType,
    EmployerInteractionChannel,
    EmployerInteractionType,
    JobStatus,
    MatchDecision,
    SuppressionScope,
)


async def _job(
    session: AsyncSession,
    source: JobSource,
    *,
    key: str,
    company: str,
    email: str | None = None,
    phone: str | None = None,
    employer_url: str | None = None,
    title: str = "Engineer",
) -> SourceJob:
    canonical = CanonicalJob(
        normalized_company=company.casefold(),
        normalized_title=title.casefold(),
        normalized_location="",
        canonical_fingerprint=key * 64,
        status=JobStatus.ACTIVE,
    )
    session.add(canonical)
    await session.flush()
    job = SourceJob(
        source_id=source.id,
        canonical_job_id=canonical.id,
        external_job_id=key,
        canonical_url=f"https://jobs.example/{key}",
        localized_urls={},
        title=title,
        company=company,
        employer_url=employer_url,
        categories_seen=[],
        cities=[],
        public_email=email,
        public_emails=[email] if email else [],
        public_phone=phone,
        public_phones=[phone] if phone else [],
        content_hash=key * 64,
        matching_content_hash=key * 64,
        source_fingerprint=key * 64,
        status=JobStatus.ACTIVE,
        raw_metadata={},
    )
    session.add(job)
    await session.flush()
    return job


def test_decline_scope_requires_explicit_attendance_for_employer_suppression() -> None:
    text = "Спасибо, я решил отказаться от вакансии в этой компании."

    assert classify_candidate_decline(text, interview_attended=False) is SuppressionScope.JOB
    assert classify_candidate_decline(text, interview_attended=True) is SuppressionScope.EMPLOYER
    assert classify_candidate_decline("Я пока подумаю", interview_attended=True) is None


async def _source(session: AsyncSession, *, adapter: str = "fixture_source") -> JobSource:
    source = JobSource(
        name="Fixture",
        base_url="https://jobs.example",
        adapter_type=adapter,
        configuration={},
    )
    session.add(source)
    await session.flush()
    return source


@pytest.mark.asyncio
async def test_exact_domain_merges_different_spellings(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session)
        first = await _job(
            session, source, key="a", company="Print Terra", email="jobs@printterra.md"
        )
        second = await _job(
            session, source, key="b", company="PRINTTERRA SRL", email="hr@printterra.md"
        )
        service = EmployerIdentityService()
        first_result = await service.resolve_for_source_job(session, first)
        second_result = await service.resolve_for_source_job(session, second)

        assert first_result.employer.id == second_result.employer.id
        assert second_result.matched_existing is True


@pytest.mark.asyncio
async def test_delucru_company_id_is_stable_across_locale_and_profile_slug(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session, adapter="delucru_md")
        first = await _job(
            session,
            source,
            key="a",
            company="Alpha",
            employer_url="https://www.delucru.md/company/alpha-42",
        )
        second = await _job(
            session,
            source,
            key="b",
            company="Alpha SRL",
            employer_url="https://www.delucru.md/ru/company/new-name-42",
        )
        first.raw_metadata = second.raw_metadata = {"company_id": "42"}
        identity = EmployerIdentityService()
        assert (await identity.resolve_for_source_job(session, first)).employer.id == (
            await identity.resolve_for_source_job(session, second)
        ).employer.id


@pytest.mark.asyncio
async def test_company_website_and_new_contacts_enrich_existing_identity(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session)
        first = await _job(
            session,
            source,
            key="a",
            company="Alpha",
            employer_url="https://www.delucru.md/company/alpha-42",
        )
        second = await _job(
            session,
            source,
            key="b",
            company="Alpha SRL",
            employer_url="https://www.rabota.md/companies/alpha",
        )
        identity = EmployerIdentityService()
        employer = (await identity.resolve_for_source_job(session, first)).employer
        first.raw_metadata = {"company_website": "https://www.alpha.md/careers"}
        first.public_email = "jobs@alpha.md"
        enriched = await identity.resolve_for_source_job(session, first)
        second.raw_metadata = {"company_website": "https://alpha.md"}
        assert enriched.employer.id == employer.id
        assert (await identity.resolve_for_source_job(session, second)).employer.id == employer.id


@pytest.mark.asyncio
async def test_exact_external_company_profile_resolves_across_sources(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        first_source, second_source = await _source(session), await _source(session)
        second_source.base_url = "https://another-board.example"
        first = await _job(
            session,
            first_source,
            key="external-a",
            company="Alpha",
            email="first@gmail.com",
            employer_url="https://alpha.example/careers",
        )
        second = await _job(
            session,
            second_source,
            key="external-b",
            company="Alpha SRL",
            email="second@gmail.com",
            employer_url="https://alpha.example/careers",
        )
        service = EmployerIdentityService()
        first_result = await service.resolve_for_source_job(session, first)
        second_result = await service.resolve_for_source_job(session, second)
        assert first_result.employer.id == second_result.employer.id
        assert not second_result.ambiguous


@pytest.mark.asyncio
async def test_source_employer_id_merges_rabota_jobs(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session, adapter="rabota_md")
        first = await _job(
            session,
            source,
            key="c",
            company="Alpha",
            employer_url="https://www.rabota.md/ru/companies/alpha-42",
        )
        second = await _job(
            session,
            source,
            key="d",
            company="Alpha SRL",
            employer_url="https://www.rabota.md/ro/companies/alpha-42/",
        )
        service = EmployerIdentityService()
        one = await service.resolve_for_source_job(session, first)
        two = await service.resolve_for_source_job(session, second)

        assert one.employer.id == two.employer.id


@pytest.mark.asyncio
async def test_different_source_profiles_sharing_recruiter_contact_do_not_merge(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session, adapter="rabota_md")
        first = await _job(
            session,
            source,
            key="p",
            company="Client One",
            email="recruiter@agency.example",
            phone="+373 60 999 888",
            employer_url="https://www.rabota.md/ru/companies/client-one",
        )
        second = await _job(
            session,
            source,
            key="q",
            company="Client Two",
            email="recruiter@agency.example",
            phone="+373 60 999 888",
            employer_url="https://www.rabota.md/ru/companies/client-two",
        )
        service = EmployerIdentityService()
        first_result = await service.resolve_for_source_job(session, first)
        second_result = await service.resolve_for_source_job(session, second)

        assert first_result.employer.id != second_result.employer.id


@pytest.mark.asyncio
async def test_shared_recruiter_without_profiles_requires_identity_review(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session)
        first = await _job(
            session,
            source,
            key="agency-a",
            company="Client One",
            email="recruiter@gmail.com",
            phone="+37360004589",
        )
        second = await _job(
            session,
            source,
            key="agency-b",
            company="Client Two",
            email="recruiter@gmail.com",
            phone="+37360004589",
        )
        service = EmployerIdentityService()
        first_result = await service.resolve_for_source_job(session, first)
        second_result = await service.resolve_for_source_job(session, second)
        assert first_result.employer.id != second_result.employer.id
        assert second_result.ambiguous


@pytest.mark.asyncio
async def test_source_website_and_shared_email_provider_are_not_company_domains(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session)
        item = await _job(
            session,
            source,
            key="board-site",
            company="Gmail",
            email="recruiter@gmail.com",
            employer_url="https://jobs.example/company/gmail",
        )
        item.raw_metadata = {"company_website": source.base_url}
        signals = await EmployerIdentityService().signals_for_source_job(session, item)
        assert not any(
            signal.identifier_type == EmployerIdentifierType.DOMAIN for signal in signals
        )
        assert not any(
            signal.identifier_type == EmployerIdentifierType.EMAIL and signal.namespace == "global"
            for signal in signals
        )


@pytest.mark.asyncio
async def test_long_profile_urls_keep_contact_namespace_bounded_and_distinct(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session, adapter="rabota_md")
        prefix = "https://www.rabota.md/ru/companies/" + "long-company-name-" * 10
        first = await _job(
            session,
            source,
            key="x",
            company="One",
            phone="+373 60 999 888",
            employer_url=prefix + "one",
        )
        same = await _job(
            session,
            source,
            key="y",
            company="One SRL",
            phone="+373 60 999 888",
            employer_url=prefix + "one",
        )
        other = await _job(
            session,
            source,
            key="z",
            company="Two",
            phone="+373 60 999 888",
            employer_url=prefix + "two",
        )
        service = EmployerIdentityService()
        namespaces = [
            signal.namespace
            for job in (first, same, other)
            for signal in await service.signals_for_source_job(session, job)
        ]
        assert all(len(value) <= 128 for value in namespaces)
        one = await service.resolve_for_source_job(session, first)
        same_result = await service.resolve_for_source_job(session, same)
        two = await service.resolve_for_source_job(session, other)
        assert one.employer.id == same_result.employer.id
        assert one.employer.id != two.employer.id


@pytest.mark.asyncio
async def test_similar_name_without_strong_identifier_never_merges(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session)
        first = await _job(session, source, key="e", company="Victoria")
        second = await _job(session, source, key="f", company="VICTORIA")
        service = EmployerIdentityService()

        assert (await service.resolve_for_source_job(session, first)).employer.id != (
            await service.resolve_for_source_job(session, second)
        ).employer.id


@pytest.mark.asyncio
async def test_same_name_different_domains_do_not_merge(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session)
        first = await _job(
            session, source, key="g", company="Victoria", email="hr@victoria-bank.md"
        )
        second = await _job(
            session, source, key="h", company="Victoria", email="jobs@victoria-shop.md"
        )
        service = EmployerIdentityService()

        assert (await service.resolve_for_source_job(session, first)).employer.id != (
            await service.resolve_for_source_job(session, second)
        ).employer.id


@pytest.mark.asyncio
async def test_conflicting_strong_identity_is_review_candidate_without_merge(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        source = await _source(session)
        email_job = await _job(session, source, key="i", company="One", email="jobs@one.example")
        phone_job = await _job(session, source, key="j", company="Two", phone="+373 60 111 222")
        service = EmployerIdentityService()
        one = await service.resolve_for_source_job(session, email_job)
        two = await service.resolve_for_source_job(session, phone_job)
        conflict = await _job(
            session,
            source,
            key="k",
            company="Uncertain",
            email="other@one.example",
            phone="+373 60 111 222",
        )
        result = await service.resolve_for_source_job(session, conflict)
        candidate = await session.scalar(
            select(EmployerIdentityCandidate).where(
                EmployerIdentityCandidate.source_job_id == conflict.id
            )
        )

        assert result.ambiguous is True
        assert result.employer.id not in {one.employer.id, two.employer.id}
        assert candidate is not None
        assert (
            await session.scalar(
                select(func.count(EmployerInteractionEvent.id)).where(
                    EmployerInteractionEvent.employer_id == result.employer.id
                )
            )
            == 0
        )


async def _relationship_graph(
    session: AsyncSession,
) -> tuple[
    UserProfile,
    CanonicalEmployer,
    SourceJob,
    SourceJob,
    Application,
    Application,
    MatchEvaluation,
    MatchEvaluation,
]:
    source = await _source(session)
    employer = CanonicalEmployer(normalized_name="printterra", primary_domain="printterra.md")
    profile = UserProfile(name="Candidate", is_default=True)
    session.add_all([employer, profile])
    await session.flush()
    first_job = await _job(session, source, key="l", company="Printterra", title="Receptionist")
    second_job = await _job(session, source, key="m", company="Printterra", title="Engineer")
    first_job.employer_id = employer.id
    second_job.employer_id = employer.id
    first_canonical = await session.get(CanonicalJob, first_job.canonical_job_id)
    second_canonical = await session.get(CanonicalJob, second_job.canonical_job_id)
    assert first_canonical is not None and second_canonical is not None
    first_canonical.employer_id = employer.id
    second_canonical.employer_id = employer.id
    evaluations = [
        MatchEvaluation(
            profile_id=profile.id,
            canonical_job_id=job.canonical_job_id,
            source_job_id=job.id,
            resume_fit=score,
            preference_fit=score,
            overall_fit=score,
            requirements_met=[],
            missing_requirements=[],
            risks=[],
            scam_indicators=[],
            explanation="fixture",
            decision=MatchDecision.AUTO_APPLY,
            model="fixture",
            prompt_rules_version="fixture",
        )
        for job, score in ((first_job, 90), (second_job, 80))
    ]
    session.add_all(evaluations)
    await session.flush()
    applications = [
        Application(
            profile_id=profile.id,
            canonical_job_id=job.canonical_job_id,
            employer_id=employer.id,
            source_job_id=job.id,
            match_evaluation_id=evaluation.id,
            resume_id=profile.id,
            recipient_contact_id=profile.id,
            subject="fixture",
            body="fixture",
            language="en",
            status=ApplicationStatus.PREPARED,
            idempotency_key=key * 64,
        )
        for job, evaluation, key in (
            (first_job, evaluations[0], "n"),
            (second_job, evaluations[1], "o"),
        )
    ]
    # The policy-only tests never dereference resume/contact, and SQLite keeps
    # foreign-key enforcement disabled for this in-memory unit fixture.
    session.add_all(applications)
    await session.flush()
    return (
        profile,
        employer,
        first_job,
        second_job,
        applications[0],
        applications[1],
        evaluations[0],
        evaluations[1],
    )


@pytest.mark.asyncio
async def test_best_vacancy_gets_only_employer_slot_and_reply_freezes_all(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        (
            profile,
            employer,
            first_job,
            second_job,
            first,
            second,
            first_eval,
            second_eval,
        ) = await _relationship_graph(session)
        service = EmployerRelationshipService()
        assert (
            await service.policy_outcome(
                session, application=first, evaluation=first_eval, job=first_job
            )
        ).slot_available is True
        assert (
            await service.policy_outcome(
                session, application=second, evaluation=second_eval, job=second_job
            )
        ).slot_available is False
        await service.record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.EMPLOYER_REPLIED,
            channel=EmployerInteractionChannel.EMAIL,
            idempotency_key="reply-1",
        )
        assert (
            await service.policy_outcome(
                session, application=first, evaluation=first_eval, job=first_job
            )
        ).no_active_conversation is False


@pytest.mark.asyncio
async def test_job_decline_is_narrow_employer_decline_is_global_and_reopen_preserves_history(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        (
            profile,
            employer,
            first_job,
            second_job,
            first,
            second,
            first_eval,
            second_eval,
        ) = await _relationship_graph(session)
        service = EmployerRelationshipService()
        await service.suppress(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            scope=SuppressionScope.JOB,
            canonical_job_id=first.canonical_job_id,
            reason="not this vacancy",
            actor="owner",
        )
        first_outcome = await service.policy_outcome(
            session, application=first, evaluation=first_eval, job=first_job
        )
        second_outcome = await service.policy_outcome(
            session, application=second, evaluation=second_eval, job=second_job
        )
        assert first_outcome.not_suppressed is False
        assert second_outcome.not_suppressed is True

        await service.suppress(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            scope=SuppressionScope.EMPLOYER,
            reason="declined after interview",
            actor="owner",
        )
        assert (
            await service.policy_outcome(
                session, application=second, evaluation=second_eval, job=second_job
            )
        ).not_suppressed is False
        await service.reopen(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            reason="owner reopened",
            actor="owner",
        )
        assert (
            await service.policy_outcome(
                session, application=second, evaluation=second_eval, job=second_job
            )
        ).not_suppressed is True
        assert (
            await session.scalar(
                select(func.count(EmployerInteractionEvent.id)).where(
                    EmployerInteractionEvent.employer_id == employer.id,
                    EmployerInteractionEvent.event_type
                    == EmployerInteractionType.CANDIDATE_DECLINED_EMPLOYER,
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_relationship_event_ingestion_is_idempotent(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        profile, employer, *_rest = await _relationship_graph(session)
        service = EmployerRelationshipService()
        first, created = await service.record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.INTERVIEW_CONFIRMED,
            channel=EmployerInteractionChannel.CALL,
            idempotency_key="appointment:stable-id",
            occurred_at=datetime(2026, 9, 20, tzinfo=UTC),
        )
        second, duplicate_created = await service.record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.INTERVIEW_CONFIRMED,
            channel=EmployerInteractionChannel.CALL,
            idempotency_key="appointment:stable-id",
            occurred_at=datetime(2026, 9, 20, tzinfo=UTC),
        )

        assert created is True
        assert duplicate_created is False
        assert first.id == second.id


@pytest.mark.asyncio
async def test_role_family_suppression_ignores_seniority_but_allows_other_occupation(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        (
            profile,
            employer,
            first_job,
            second_job,
            _first,
            second,
            first_eval,
            second_eval,
        ) = await _relationship_graph(session)
        first_job.title = "Senior Receptionist"
        second_job.title = "Receptionist full time"
        service = EmployerRelationshipService()
        await service.suppress(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            scope=SuppressionScope.ROLE_FAMILY,
            role=first_job.title,
            reason="no receptionist roles",
            actor="owner",
        )
        blocked = await service.policy_outcome(
            session, application=second, evaluation=second_eval, job=second_job
        )
        assert blocked.not_suppressed is False

        second_job.title = "Engineer"
        unrelated = await service.policy_outcome(
            session, application=second, evaluation=second_eval, job=second_job
        )
        assert unrelated.not_suppressed is True
        assert first_eval.id != second_eval.id


@pytest.mark.asyncio
async def test_interview_freezes_then_employer_rejection_releases_deferred_slot(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        (
            profile,
            employer,
            _first_job,
            second_job,
            _first,
            second,
            _first_eval,
            second_eval,
        ) = await _relationship_graph(session)
        service = EmployerRelationshipService()
        await service.record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.INTERVIEW_CONFIRMED,
            channel=EmployerInteractionChannel.CALL,
            idempotency_key="interview-confirmed",
        )
        frozen = await service.policy_outcome(
            session, application=second, evaluation=second_eval, job=second_job
        )
        assert frozen.no_active_conversation is False

        await service.record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.EMPLOYER_REJECTED,
            channel=EmployerInteractionChannel.EMAIL,
            idempotency_key="employer-rejected",
        )
        released = await service.policy_outcome(
            session, application=second, evaluation=second_eval, job=second_job
        )
        assert released.no_active_conversation is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("closing_event", "scope"),
    [
        (EmployerInteractionType.EMPLOYER_REJECTED, SuppressionScope.NONE),
        (EmployerInteractionType.RELATIONSHIP_REOPENED, SuppressionScope.NONE),
        (EmployerInteractionType.CANDIDATE_DECLINED_JOB, SuppressionScope.JOB),
    ],
)
async def test_finished_relationship_releases_prior_sent_application_slot(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    closing_event: EmployerInteractionType,
    scope: SuppressionScope,
) -> None:
    async with sqlite_session_factory() as session:
        (
            profile,
            employer,
            _first_job,
            second_job,
            first,
            second,
            _first_eval,
            second_eval,
        ) = await _relationship_graph(session)
        first.status = ApplicationStatus.SENT
        first.sent_at = datetime(2026, 9, 21, 9, tzinfo=UTC)
        service = EmployerRelationshipService()
        await service.record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.APPLICATION_SENT,
            channel=EmployerInteractionChannel.APPLICATION,
            idempotency_key="first-sent-before-rejection",
            application_id=first.id,
            occurred_at=first.sent_at,
        )
        pending = await service.policy_outcome(
            session, application=second, evaluation=second_eval, job=second_job
        )
        assert pending.slot_available is False

        await service.record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=closing_event,
            channel=EmployerInteractionChannel.EMAIL,
            idempotency_key=f"first-closed-before-second:{closing_event.value}",
            suppression_scope=scope,
            canonical_job_id=first.canonical_job_id if scope is SuppressionScope.JOB else None,
        )
        released = await service.policy_outcome(
            session, application=second, evaluation=second_eval, job=second_job
        )
        assert released.slot_available is True
        assert first.status is ApplicationStatus.SENT


@pytest.mark.asyncio
async def test_permanent_delivery_failure_releases_slot_without_rewriting_sent_fact(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        (
            profile,
            employer,
            first_job,
            second_job,
            first,
            second,
            _first_eval,
            second_eval,
        ) = await _relationship_graph(session)
        first.status = ApplicationStatus.SENT
        first.sent_at = datetime(2026, 9, 16, 21, 4, 49, tzinfo=UTC)
        delivery = EmailDelivery(
            application_id=first.id,
            provider="gmail",
            recipient="redacted@sincer.md",
            status=DeliveryStatus.RECIPIENT_REJECTED,
            sanitized_provider_response={},
            attempt_count=1,
        )
        session.add(delivery)
        service = EmployerRelationshipService()
        await service.record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.APPLICATION_SENT,
            channel=EmployerInteractionChannel.EMAIL,
            idempotency_key="historical-send",
            application_id=first.id,
            occurred_at=first.sent_at,
        )
        await service.record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.APPLICATION_DELIVERY_FAILED,
            channel=EmployerInteractionChannel.EMAIL,
            idempotency_key="historical-bounce",
            application_id=first.id,
            occurred_at=datetime(2026, 9, 17, tzinfo=UTC),
        )
        outcome = await service.policy_outcome(
            session, application=second, evaluation=second_eval, job=second_job
        )
        assert outcome.slot_available is True
        assert first.status is ApplicationStatus.SENT
        assert first_job.employer_id == employer.id


@pytest.mark.asyncio
async def test_printerra_backfill_fixture_preserves_sent_rows_and_reports_rapid_sends(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        (
            _profile,
            _employer,
            first_job,
            second_job,
            first,
            second,
            *_rest,
        ) = await _relationship_graph(session)
        first_job.company = "Printerra"
        second_job.company = "Printerra"
        first_job.employer_url = "https://www.rabota.md/ru/companies/printerra"
        second_job.employer_url = "https://www.rabota.md/ru/companies/printerra"
        first.status = ApplicationStatus.SENT
        second.status = ApplicationStatus.SENT
        first.sent_at = datetime(2026, 9, 21, 21, 0, 45, tzinfo=UTC)
        second.sent_at = datetime(2026, 9, 21, 21, 0, 46, tzinfo=UTC)
        before = [
            (first.id, first.status, first.sent_at),
            (second.id, second.status, second.sent_at),
        ]

        result = await EmployerBackfillService().apply(session)
        audit = await EmployerSafetyAuditService().report(session, company_filter="Printerra")
        after = [
            (first.id, first.status, first.sent_at),
            (second.id, second.status, second.sent_at),
        ]

        assert result["events_created"] == 2
        assert before == after
        assert audit["historical_sent_mutations"] == 0
        assert audit["A_same_employer_within_24h"][0]["window_seconds"] == 1
        assert audit["focused_evidence"]["communications"] == []


@pytest.mark.asyncio
async def test_retro_audit_uses_relationship_state_at_each_send_time(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        (
            profile,
            employer,
            first_job,
            _second_job,
            first,
            second,
            *_rest,
        ) = await _relationship_graph(session)
        first.status = second.status = ApplicationStatus.SENT
        first.sent_at = datetime(2026, 9, 21, 9, tzinfo=UTC)
        service = EmployerRelationshipService()
        for event_type, hour, scope in (
            (EmployerInteractionType.EMPLOYER_REPLIED, 10, SuppressionScope.NONE),
            (EmployerInteractionType.CANDIDATE_DECLINED_EMPLOYER, 11, SuppressionScope.EMPLOYER),
            (EmployerInteractionType.RELATIONSHIP_REOPENED, 12, SuppressionScope.NONE),
            (EmployerInteractionType.CANDIDATE_DECLINED_JOB, 13, SuppressionScope.JOB),
        ):
            await service.record_event(
                session,
                profile_id=profile.id,
                employer_id=employer.id,
                event_type=event_type,
                channel=EmployerInteractionChannel.MANUAL,
                idempotency_key=f"audit-event:{hour}",
                occurred_at=datetime(2026, 9, 21, hour, tzinfo=UTC),
                suppression_scope=scope,
                canonical_job_id=(
                    first_job.canonical_job_id if scope is SuppressionScope.JOB else None
                ),
            )

        second.sent_at = datetime(2026, 9, 21, 10, 30, tzinfo=UTC)
        during_reply = await EmployerSafetyAuditService().report(session)
        assert [
            row["application_id"] for row in during_reply["C_sent_during_active_conversation"]
        ] == [str(second.id)]
        assert during_reply["B_sent_after_candidate_decline"] == []

        second.sent_at = datetime(2026, 9, 21, 11, 30, tzinfo=UTC)
        after_decline = await EmployerSafetyAuditService().report(session)
        assert [
            row["application_id"] for row in after_decline["B_sent_after_candidate_decline"]
        ] == [str(second.id)]
        assert after_decline["C_sent_during_active_conversation"] == []

        second.sent_at = datetime(2026, 9, 21, 12, 30, tzinfo=UTC)
        after_reopen = await EmployerSafetyAuditService().report(session)
        assert after_reopen["B_sent_after_candidate_decline"] == []
        assert after_reopen["C_sent_during_active_conversation"] == []

        second.sent_at = datetime(2026, 9, 21, 13, 30, tzinfo=UTC)
        after_other_job_decline = await EmployerSafetyAuditService().report(session)
        assert after_other_job_decline["B_sent_after_candidate_decline"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "scope", "incident_type"),
    [
        (
            EmployerInteractionType.EMPLOYER_REPLIED,
            SuppressionScope.NONE,
            "sent_during_active_conversation",
        ),
        (
            EmployerInteractionType.CANDIDATE_DECLINED_EMPLOYER,
            SuppressionScope.EMPLOYER,
            "sent_after_candidate_decline",
        ),
    ],
)
async def test_historical_send_incidents_are_persistent_idempotent_and_preserve_sent(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    event_type: EmployerInteractionType,
    scope: SuppressionScope,
    incident_type: str,
) -> None:
    async with sqlite_session_factory() as session:
        (
            profile,
            employer,
            _first_job,
            _second_job,
            first,
            second,
            *_rest,
        ) = await _relationship_graph(session)
        first.status = second.status = ApplicationStatus.SENT
        first.sent_at = datetime(2026, 9, 21, 9, tzinfo=UTC)
        second.sent_at = datetime(2026, 9, 21, 11, tzinfo=UTC)
        await EmployerRelationshipService().record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=event_type,
            channel=EmployerInteractionChannel.MANUAL,
            idempotency_key=f"historical-incident:{event_type.value}",
            occurred_at=datetime(2026, 9, 21, 10, tzinfo=UTC),
            suppression_scope=scope,
        )
        service = EmployerSafetyAuditService()
        first_result = await service.record_historical_incidents(session)
        first_id, second_id = first.id, second.id
        await session.commit()

    async with sqlite_session_factory() as session:
        second_result = await service.record_historical_incidents(session)
        incidents = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "employer.historical_send_incident")
            )
        ).all()

        assert first_result["rapid_same_employer_send"] == 1
        assert first_result[incident_type] == 1
        assert all(count == 0 for count in second_result.values())
        assert {incident.decision for incident in incidents} == {
            "rapid_same_employer_send",
            incident_type,
        }
        assert {incident.entity_id for incident in incidents} == {str(second_id)}
        first = await session.get(Application, first_id)
        second = await session.get(Application, second_id)
        assert first is not None and second is not None
        assert first.status is second.status is ApplicationStatus.SENT
        assert first.sent_at is not None and first.sent_at.replace(tzinfo=UTC) == datetime(
            2026, 9, 21, 9, tzinfo=UTC
        )
        assert second.sent_at is not None and second.sent_at.replace(tzinfo=UTC) == datetime(
            2026, 9, 21, 11, tzinfo=UTC
        )


@pytest.mark.asyncio
async def test_retro_remediation_never_mutates_historical_sent_application(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        (
            profile,
            employer,
            _first_job,
            _second_job,
            first,
            second,
            *_rest,
        ) = await _relationship_graph(session)
        first.status = ApplicationStatus.SENT
        first.sent_at = datetime(2026, 9, 2, 21, 0, 47, tzinfo=UTC)
        await EmployerRelationshipService().suppress(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            scope=SuppressionScope.EMPLOYER,
            reason="declined after interview",
            actor="owner",
        )
        result = await EmployerSafetyAuditService().remediate_unsent(session)

        assert result == {"cancelled": 1, "deferred": 0, "historical_sent_mutations": 0}
        assert first.status is ApplicationStatus.SENT
        assert first.sent_at == datetime(2026, 9, 2, 21, 0, 47, tzinfo=UTC)
        assert second.status is ApplicationStatus.CANCELLED


@pytest.mark.asyncio
async def test_relationship_event_enqueues_policy_refresh(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        profile, employer, *_ = await _relationship_graph(session)
        await EmployerRelationshipService().record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.INTERVIEW_CONFIRMED,
            channel=EmployerInteractionChannel.CALL,
            idempotency_key="dirty-refresh:test",
        )
        marker = await session.scalar(
            select(ApplicationPolicyRefreshQueue).where(
                ApplicationPolicyRefreshQueue.profile_id == profile.id,
                ApplicationPolicyRefreshQueue.employer_id == employer.id,
            )
        )
        assert marker is not None
        assert marker.reason == "employer_event:interview_confirmed"

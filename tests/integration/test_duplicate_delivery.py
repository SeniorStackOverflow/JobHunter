from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.email.providers import FakeGmailProvider
from app.email.service import EmailSendBlocked, EmailService
from app.employers.identity import EmployerIdentityService
from app.matching.source_version import compute_source_matching_hash
from app.models.entities import (
    Application,
    CanonicalEmployer,
    EmployerRelationship,
)
from app.models.enums import ApplicationStatus, PolicyDecision
from tests.unit.test_policy_and_email import make_graph, settings


def clone(obj, **overrides):
    values = {
        column.name: deepcopy(getattr(obj, column.name))
        for column in obj.__table__.columns
        if column.name not in {"id", "created_at", "updated_at"}
    }
    return type(obj)(**{**values, **overrides})


async def legacy_duplicate_graph(session, storage: Path):
    values = await make_graph(session, storage)
    source, _profile, _preference, _resume, canonical, job, evaluation, contact, application = (
        values
    )
    employer = (await EmployerIdentityService().resolve_for_source_job(session, job)).employer
    canonical.employer_id = contact.employer_id = application.employer_id = employer.id
    application.status = ApplicationStatus.AUTO_APPROVED
    application.policy_decision = PolicyDecision.AUTO_APPROVED
    # Reproduce legacy separate canonical/employer IDs rather than allowing the
    # new scanner to merge the duplicates before the sender can be exercised.
    other_source = clone(source, name="Legacy mirror", base_url="https://mirror.example.test")
    other_employer = CanonicalEmployer(normalized_name="example company")
    other_canonical = clone(
        canonical,
        canonical_fingerprint=uuid4().hex * 2,
        normalized_company="example company",
        primary_source_job_id=None,
        employer_id=None,
    )
    session.add_all([other_source, other_employer, other_canonical])
    await session.flush()
    other_canonical.employer_id = other_employer.id
    other_job = clone(
        job,
        source_id=other_source.id,
        canonical_job_id=other_canonical.id,
        employer_id=other_employer.id,
        external_job_id="legacy-mirror",
        canonical_url="https://mirror.example.test/job/legacy-mirror",
        company="Example Company SRL",
    )
    session.add(other_job)
    await session.flush()
    other_canonical.primary_source_job_id = other_job.id
    other_job.matching_content_hash = compute_source_matching_hash(other_job)
    other_evaluation = clone(
        evaluation,
        canonical_job_id=other_canonical.id,
        source_job_id=other_job.id,
        source_matching_hash=other_job.matching_content_hash,
    )
    other_contact = clone(
        contact,
        canonical_job_id=other_canonical.id,
        source_job_id=other_job.id,
        employer_id=other_employer.id,
        evidence_url=other_job.canonical_url,
    )
    session.add_all([other_evaluation, other_contact])
    await session.flush()
    other_application = clone(
        application,
        canonical_job_id=other_canonical.id,
        employer_id=other_employer.id,
        source_job_id=other_job.id,
        match_evaluation_id=other_evaluation.id,
        recipient_contact_id=other_contact.id,
        idempotency_key=uuid4().hex * 2,
    )
    session.add(other_application)
    await session.flush()
    return application.id, other_application.id


@pytest.mark.asyncio
async def test_legacy_duplicate_is_blocked_after_release_window_even_with_manual_approval(
    sqlite_session_factory, tmp_path: Path
):
    async with sqlite_session_factory() as session:
        first_id, second_id = await legacy_duplicate_graph(session, tmp_path)
        await session.commit()
    provider = FakeGmailProvider()
    sender = EmailService(settings(tmp_path), sqlite_session_factory, provider)
    await sender.send_application(first_id)
    async with sqlite_session_factory() as session:
        first = await session.get(Application, first_id)
        first.sent_at = datetime.now(UTC) - timedelta(days=20)
        relationship = await session.scalar(
            select(EmployerRelationship).where(
                EmployerRelationship.employer_id == first.employer_id
            )
        )
        relationship.last_application_at = first.sent_at
        relationship.last_interaction_at = first.sent_at
        second = await session.get(Application, second_id)
        second.status = ApplicationStatus.APPROVED
        await session.commit()
    with pytest.raises(EmailSendBlocked, match="manual approval"):
        await sender.send_application(second_id)
    assert len(provider.outbox) == 1
    async with sqlite_session_factory() as session:
        second = await session.get(Application, second_id)
        assert second.status is ApplicationStatus.BLOCKED
        assert "no_duplicate_application" in second.policy_result["rules_failed"]

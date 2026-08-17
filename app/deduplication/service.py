from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers.parsing.normalization import normalize_for_fingerprint, stable_hash
from app.models.entities import CanonicalJob, SourceJob
from app.models.enums import JobStatus


def token_similarity(left: str | None, right: str | None) -> float:
    left_tokens = set(normalize_for_fingerprint(left).split())
    right_tokens = set(normalize_for_fingerprint(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def employer_domain(job: SourceJob) -> str | None:
    if job.public_email and "@" in job.public_email:
        return job.public_email.rsplit("@", maxsplit=1)[1].lower()
    if job.employer_url:
        return urlsplit(job.employer_url).hostname
    return None


def canonical_fingerprint(job: SourceJob) -> str:
    return stable_hash(
        normalize_for_fingerprint(job.company),
        normalize_for_fingerprint(job.title),
        normalize_for_fingerprint(job.location),
        employer_domain(job),
    )


@dataclass(frozen=True)
class DeduplicationResult:
    canonical_job: CanonicalJob
    merged_existing: bool
    reasons: tuple[str, ...]


class DeduplicationService:
    async def assign(self, session: AsyncSession, job: SourceJob) -> DeduplicationResult:
        if job.canonical_job_id:
            current = await session.get(CanonicalJob, job.canonical_job_id)
            if current is not None:
                return DeduplicationResult(current, True, ("already_assigned",))

        fingerprint = canonical_fingerprint(job)
        exact = await session.scalar(
            select(CanonicalJob).where(CanonicalJob.canonical_fingerprint == fingerprint)
        )
        if exact is not None:
            job.canonical_job_id = exact.id
            await session.flush()
            return DeduplicationResult(exact, True, ("canonical_fingerprint",))

        candidates = (
            await session.scalars(
                select(SourceJob).where(
                    SourceJob.canonical_job_id.is_not(None), SourceJob.status != JobStatus.CLOSED
                )
            )
        ).all()
        for candidate in candidates:
            company_match = token_similarity(job.company, candidate.company) >= 0.9
            title_match = token_similarity(job.title, candidate.title) >= 0.8
            location_match = (
                not job.location
                or not candidate.location
                or token_similarity(job.location, candidate.location) >= 0.5
            )
            contact_match = bool(
                job.public_email
                and candidate.public_email
                and job.public_email.casefold() == candidate.public_email.casefold()
            )
            content_match = job.content_hash == candidate.content_hash
            domain_match = bool(
                employer_domain(job) and employer_domain(job) == employer_domain(candidate)
            )
            if (
                company_match
                and title_match
                and location_match
                and (contact_match or content_match or domain_match)
            ):
                canonical = await session.get(CanonicalJob, candidate.canonical_job_id)
                if canonical is not None:
                    job.canonical_job_id = canonical.id
                    await session.flush()
                    reasons = tuple(
                        name
                        for name, matched in (
                            ("company_title_location", True),
                            ("public_contact", contact_match),
                            ("content_hash", content_match),
                            ("employer_domain", domain_match),
                        )
                        if matched
                    )
                    return DeduplicationResult(canonical, True, reasons)

        canonical = CanonicalJob(
            normalized_company=normalize_for_fingerprint(job.company),
            normalized_title=normalize_for_fingerprint(job.title),
            normalized_location=normalize_for_fingerprint(job.location),
            canonical_fingerprint=fingerprint,
            status=job.status,
        )
        session.add(canonical)
        await session.flush()
        canonical.primary_source_job_id = job.id
        job.canonical_job_id = canonical.id
        await session.flush()
        return DeduplicationResult(canonical, False, ("new_canonical",))

    async def split(self, session: AsyncSession, job: SourceJob) -> CanonicalJob:
        canonical = CanonicalJob(
            normalized_company=normalize_for_fingerprint(job.company),
            normalized_title=normalize_for_fingerprint(job.title),
            normalized_location=normalize_for_fingerprint(job.location),
            canonical_fingerprint=stable_hash(canonical_fingerprint(job), str(job.id)),
            primary_source_job_id=job.id,
            status=job.status,
        )
        session.add(canonical)
        await session.flush()
        job.canonical_job_id = canonical.id
        await session.flush()
        return canonical

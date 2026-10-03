from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.crawlers.parsing.normalization import normalize_for_fingerprint, stable_hash
from app.deduplication.comparison import cities, compare_jobs, domains, emails, phones, role_key
from app.employers.identity import EmployerIdentityService
from app.employers.normalization import company_key
from app.models.entities import Application, CanonicalJob, EmployerIdentifier, SourceJob
from app.models.enums import ApplicationStatus, EmployerIdentifierType, JobStatus

DEDUPLICATION_VERSION = 2
MAX_CANDIDATES = 500


def employer_domain(job: SourceJob) -> str | None:
    return next(iter(sorted(domains(job))), None)


def canonical_fingerprint(job: SourceJob) -> str:
    """A versioned content key; short company/title keys only locate candidates."""
    return stable_hash(
        DEDUPLICATION_VERSION,
        company_key(job.company),
        role_key(job.title),
        sorted(cities(job)),
        str(job.employer_id) if job.employer_id else job.employer_url,
        sorted(emails(job)),
        sorted(phones(job)),
        *(
            normalize_for_fingerprint(getattr(job, field))
            for field in (
                "description",
                "requirements",
                "responsibilities",
                "schedule",
                "employment_type",
                "required_experience",
                "workplace_type",
            )
        ),
        job.no_experience,
        job.salary_min,
        job.salary_max,
        job.currency,
    )


@dataclass(frozen=True)
class DeduplicationResult:
    canonical_job: CanonicalJob
    merged_existing: bool
    reasons: tuple[str, ...]


class DeduplicationService:
    @staticmethod
    async def _refresh_previous(
        session: AsyncSession, previous_id: UUID | None, current_id: UUID
    ) -> None:
        if previous_id is None or previous_id == current_id:
            return
        await session.flush()
        previous = await session.get(CanonicalJob, previous_id)
        if previous is None:
            return
        peers = list(
            (
                await session.scalars(
                    select(SourceJob)
                    .where(SourceJob.canonical_job_id == previous_id)
                    .order_by(SourceJob.id)
                )
            ).all()
        )
        statuses = {peer.status for peer in peers}
        previous.status = next(
            (
                status
                for status in (JobStatus.ACTIVE, JobStatus.POSSIBLY_CLOSED, JobStatus.INCOMPLETE)
                if status in statuses
            ),
            JobStatus.CLOSED,
        )
        if previous.primary_source_job_id not in {peer.id for peer in peers}:
            previous.primary_source_job_id = next(
                (peer.id for peer in peers if peer.status == previous.status), None
            )

    @staticmethod
    async def lock_profile(session: AsyncSession, profile_id: UUID) -> None:
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"jobhunter:duplicate-delivery:{profile_id}"},
            )

    async def candidate_jobs(self, session: AsyncSession, job: SourceJob) -> list[SourceJob]:
        contact_signals = []
        if emails(job):
            contact_signals.append(
                (EmployerIdentifier.identifier_type == EmployerIdentifierType.EMAIL)
                & EmployerIdentifier.normalized_value.in_(emails(job))
            )
        if phones(job):
            contact_signals.append(
                (EmployerIdentifier.identifier_type == EmployerIdentifierType.PHONE)
                & EmployerIdentifier.normalized_value.in_(phones(job))
            )
        if domains(job):
            contact_signals.append(
                (EmployerIdentifier.identifier_type == EmployerIdentifierType.DOMAIN)
                & EmployerIdentifier.normalized_value.in_(domains(job))
            )
        predicates: list[ColumnElement[bool]] = []
        names = {
            value
            for value in (company_key(job.company), normalize_for_fingerprint(job.company))
            if value
        }
        if names:
            company_ids = select(CanonicalJob.id).where(CanonicalJob.normalized_company.in_(names))
            predicates.append(SourceJob.canonical_job_id.in_(company_ids))
        if job.employer_id:
            predicates.append(SourceJob.employer_id == job.employer_id)
        if job.canonical_job_id:
            predicates.append(SourceJob.canonical_job_id == job.canonical_job_id)
        if contact_signals:
            employer_ids = select(EmployerIdentifier.employer_id).where(or_(*contact_signals))
            predicates.append(SourceJob.employer_id.in_(employer_ids))
        if not predicates:
            return []
        return list(
            (
                await session.scalars(
                    select(SourceJob)
                    .where(
                        SourceJob.id != job.id,
                        SourceJob.canonical_job_id.is_not(None),
                        or_(*predicates),
                    )
                    .order_by(SourceJob.id)
                    .limit(MAX_CANDIDATES + 1)
                )
            ).all()
        )

    @staticmethod
    def _record(
        job: SourceJob, *, status: str, reasons: tuple[str, ...], candidates: list[str]
    ) -> None:
        metadata = dict(job.raw_metadata or {})
        previous = metadata.get("deduplication")
        decision = {
            "version": DEDUPLICATION_VERSION,
            "status": status,
            "reasons": list(reasons),
            "candidate_canonical_ids": sorted(set(candidates)),
        }
        if previous != decision:
            history = list(metadata.get("deduplication_history", []))
            if isinstance(previous, dict):
                history.append(previous)
            metadata["deduplication_history"] = history[-10:]
        metadata["deduplication"] = decision
        job.raw_metadata = metadata

    async def assign(self, session: AsyncSession, job: SourceJob) -> DeduplicationResult:
        current = (
            await session.get(CanonicalJob, job.canonical_job_id) if job.canonical_job_id else None
        )
        decision = (job.raw_metadata or {}).get("deduplication", {})
        if (
            current is not None
            and isinstance(decision, dict)
            and decision.get("status") in {"manual_split", "manual_duplicate"}
            and decision.get("reviewed_matching_hash") == job.matching_content_hash
        ):
            return DeduplicationResult(current, True, (decision["status"],))
        identity = await EmployerIdentityService().resolve_for_source_job(session, job)
        fingerprint = canonical_fingerprint(job)
        if session.get_bind().dialect.name == "postgresql":
            # Shared contacts, including secondary addresses, serialize assignment.
            lock_keys = sorted(
                {
                    fingerprint,
                    *(f"email:{value}" for value in emails(job)),
                    *(f"phone:{value}" for value in phones(job)),
                    *(f"domain:{value}" for value in domains(job)),
                }
            )
            for key in lock_keys:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"jobhunter:dedup:{key}"},
                )

        candidates = await self.candidate_jobs(session, job)
        ranked = [(compare_jobs(job, candidate), candidate) for candidate in candidates]
        matches = [(result, candidate) for result, candidate in ranked if result.duplicate]
        review_ids = [
            str(candidate.canonical_job_id) for result, candidate in ranked if result.needs_review
        ]
        too_many = len(candidates) > MAX_CANDIDATES
        target_ids = {candidate.canonical_job_id for _, candidate in matches}
        reasons: tuple[str, ...] = ("new_canonical",)
        target = None
        if len(target_ids) == 1 and not too_many and not identity.ambiguous:
            best, candidate = max(matches, key=lambda item: (item[0].score, str(item[1].id)))
            target = await session.get(CanonicalJob, candidate.canonical_job_id)
            reasons = best.reasons
        elif matches:
            review_ids.extend(str(value) for value in target_ids)

        has_history = (
            current is not None
            and await session.scalar(
                select(Application.id).where(Application.canonical_job_id == current.id).limit(1)
            )
            is not None
        )
        history_conflict = False
        if current is not None and has_history:
            # Never rewrite application or delivery bindings during a later merge.
            if target is not None and target.id != current.id:
                review_ids.append(str(target.id))
                reasons = ("existing_application_history",)
            history_conflict = any(
                candidate.canonical_job_id == current.id and not result.duplicate
                for result, candidate in ranked
            )
            target = current
        if current is not None and target is None:
            current_peers = [
                candidate for candidate in candidates if candidate.canonical_job_id == current.id
            ]
            if not current_peers:
                target = current
                reasons = ("identity_enriched",)
            elif any(not compare_jobs(job, peer).duplicate for peer in current_peers):
                reasons = ("different_conditions_split",)

        if target is None:
            occupied = await session.scalar(
                select(CanonicalJob.id).where(CanonicalJob.canonical_fingerprint == fingerprint)
            )
            if occupied is not None:
                fingerprint = stable_hash(fingerprint, str(job.id))
            target = CanonicalJob(
                normalized_company=company_key(job.company),
                normalized_title=role_key(job.title),
                normalized_location=" ".join(sorted(cities(job))),
                canonical_fingerprint=fingerprint,
                primary_source_job_id=job.id,
                employer_id=job.employer_id,
                status=job.status,
            )
            session.add(target)
            await session.flush()

        job.canonical_job_id = target.id
        await self._refresh_previous(session, current.id if current else None, target.id)
        review_ids = [value for value in review_ids if value != str(target.id)]
        needs_review = bool(review_ids) or too_many or identity.ambiguous or history_conflict
        self._record(
            job,
            status="needs_review" if needs_review else "assigned",
            reasons=("candidate_budget_exceeded",) if too_many else reasons,
            candidates=review_ids,
        )
        await session.flush()
        return DeduplicationResult(target, target.primary_source_job_id != job.id, reasons)

    async def split(self, session: AsyncSession, job: SourceJob) -> CanonicalJob:
        previous_id = job.canonical_job_id
        previous = (job.raw_metadata or {}).get("deduplication", {})
        if (
            isinstance(previous, dict)
            and previous.get("status") == "manual_split"
            and previous.get("reviewed_matching_hash") == job.matching_content_hash
            and previous_id
        ):
            existing = await session.get(CanonicalJob, previous_id)
            if existing is not None:
                return existing
        distinct_ids = (
            list(previous.get("candidate_canonical_ids", [])) if isinstance(previous, dict) else []
        )
        if previous_id:
            distinct_ids.append(str(previous_id))
        canonical = CanonicalJob(
            normalized_company=company_key(job.company),
            normalized_title=role_key(job.title),
            normalized_location=" ".join(sorted(cities(job))),
            employer_id=job.employer_id,
            canonical_fingerprint=stable_hash(
                canonical_fingerprint(job), str(job.id), "manual_split"
            ),
            primary_source_job_id=job.id,
            status=job.status,
        )
        if await session.scalar(
            select(Application.id).where(Application.source_job_id == job.id).limit(1)
        ):
            raise ValueError("cannot split a publication with application history")
        session.add(canonical)
        await session.flush()
        job.canonical_job_id = canonical.id
        await self._refresh_previous(session, previous_id, canonical.id)
        self._record(job, status="manual_split", reasons=("operator_split",), candidates=[])
        job.raw_metadata = {
            **job.raw_metadata,
            "deduplication": {
                **job.raw_metadata["deduplication"],
                "reviewed_matching_hash": job.matching_content_hash,
                "distinct_canonical_ids": sorted(set(distinct_ids)),
            },
        }
        await session.flush()
        return canonical

    async def confirm_duplicate(
        self, session: AsyncSession, job: SourceJob, target_id: UUID
    ) -> CanonicalJob:
        target = await session.get(CanonicalJob, target_id)
        if target is None:
            raise LookupError("canonical job not found")
        if not job.employer_id or target.employer_id != job.employer_id:
            raise ValueError("resolve the employer identity before confirming this duplicate")
        if await session.scalar(
            select(Application.id).where(Application.source_job_id == job.id).limit(1)
        ):
            raise ValueError("cannot merge a publication with application history")
        peers = list(
            (
                await session.scalars(
                    select(SourceJob).where(SourceJob.canonical_job_id == target_id)
                )
            ).all()
        )
        if not any(
            (comparison := compare_jobs(job, peer)).duplicate or comparison.needs_review
            for peer in peers
        ):
            raise ValueError("the selected vacancy has a different employer, role or conditions")
        previous_id = job.canonical_job_id
        job.canonical_job_id = target.id
        await self._refresh_previous(session, previous_id, target.id)
        self._record(
            job, status="manual_duplicate", reasons=("operator_confirmed_duplicate",), candidates=[]
        )
        job.raw_metadata = {
            **job.raw_metadata,
            "deduplication": {
                **job.raw_metadata["deduplication"],
                "reviewed_matching_hash": job.matching_content_hash,
            },
        }
        await session.flush()
        return target

    async def conflicts_with_delivery(
        self, session: AsyncSession, job: SourceJob, profile_id: UUID, application_id: UUID
    ) -> bool:
        """Protect legacy separate canonical IDs after an employer slot expires."""
        candidates = await self.candidate_jobs(session, job)
        if len(candidates) > MAX_CANDIDATES:
            return True
        sent_jobs = list(
            (
                await session.scalars(
                    select(SourceJob)
                    .join(Application, Application.source_job_id == SourceJob.id)
                    .where(
                        Application.profile_id == profile_id,
                        Application.id != application_id,
                        SourceJob.id.in_([candidate.id for candidate in candidates]),
                        Application.status.in_(
                            {
                                ApplicationStatus.SENDING,
                                ApplicationStatus.SENT,
                                ApplicationStatus.DELIVERY_UNKNOWN,
                            }
                        ),
                    )
                )
            ).all()
        )
        reviewed = (job.raw_metadata or {}).get("deduplication", {})
        distinct = (
            set(reviewed.get("distinct_canonical_ids", []))
            if isinstance(reviewed, dict)
            and reviewed.get("reviewed_matching_hash") == job.matching_content_hash
            else set()
        )
        return any(
            ((result := compare_jobs(job, prior)).duplicate or result.needs_review)
            and str(prior.canonical_job_id) not in distinct
            for prior in sent_jobs
        )

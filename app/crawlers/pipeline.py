from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.applications.availability import block_closed_vacancy_applications
from app.audit import record_audit_event
from app.crawlers.registry import JobSourceAdapterRegistry
from app.crawlers.schemas import NormalizedJobData, RawJobReference, ScanCheckpoint
from app.deduplication import DeduplicationService
from app.matching.source_version import (
    changes_require_rematch,
    compute_source_matching_hash,
)
from app.models.entities import (
    Alert,
    BatchScanRun,
    CanonicalJob,
    JobSnapshot,
    JobSource,
    ScanRun,
    SourceCategory,
    SourceJob,
)
from app.models.enums import JobStatus, RunStatus, ScanType, SourceHealth
from app.security.ssrf import UnsafeURLError

SOURCE_JOB_FIELDS = (
    "canonical_url",
    "title",
    "company",
    "employer_url",
    "category",
    "subcategory",
    "description",
    "requirements",
    "responsibilities",
    "salary_text",
    "salary_min",
    "salary_max",
    "currency",
    "location",
    "cities",
    "schedule",
    "employment_type",
    "required_experience",
    "no_experience",
    "workplace_type",
    "public_email",
    "public_phone",
    "public_emails",
    "public_phones",
    "application_url",
    "page_locale",
    "published_at",
    "source_updated_at",
    "content_hash",
    "source_fingerprint",
    "status",
    "raw_metadata",
)


def _degradation_reason(exc: Exception) -> str | None:
    if isinstance(exc, UnsafeURLError):
        return "adapter attempted an unsafe or non-allowlisted URL"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {403, 429}:
        return f"source returned HTTP {exc.response.status_code}"
    message = str(exc).casefold()
    if any(
        marker in message
        for marker in (
            "captcha",
            "login challenge",
            "anti-bot",
            "access policy",
        )
    ):
        return f"adapter access degraded: {type(exc).__name__}"
    return None


class ScanService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: JobSourceAdapterRegistry,
        deduplication: DeduplicationService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.deduplication = deduplication or DeduplicationService()

    async def _refresh_canonical_status(
        self,
        session: AsyncSession,
        canonical_job_ids: set[UUID],
    ) -> None:
        for canonical_job_id in canonical_job_ids:
            canonical = await session.get(CanonicalJob, canonical_job_id)
            if canonical is None:
                continue
            statuses = set(
                (
                    await session.scalars(
                        select(SourceJob.status).where(
                            SourceJob.canonical_job_id == canonical_job_id
                        )
                    )
                ).all()
            )
            if JobStatus.ACTIVE in statuses:
                canonical.status = JobStatus.ACTIVE
            elif JobStatus.POSSIBLY_CLOSED in statuses:
                canonical.status = JobStatus.POSSIBLY_CLOSED
            elif JobStatus.INCOMPLETE in statuses:
                canonical.status = JobStatus.INCOMPLETE
            elif statuses:
                canonical.status = JobStatus.CLOSED

    async def create_scan(
        self,
        source_id: UUID,
        scan_type: ScanType,
        *,
        resume_from_checkpoint: bool = True,
        actor: str = "scheduler",
    ) -> ScanRun:
        async with self.session_factory() as session:
            source = await session.scalar(
                select(JobSource).where(JobSource.id == source_id).with_for_update()
            )
            if source is None:
                raise LookupError(f"source {source_id} does not exist")
            active = await session.scalar(
                select(ScanRun)
                .where(
                    ScanRun.source_id == source_id,
                    ScanRun.scan_type == scan_type,
                    ScanRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
                )
                .order_by(desc(func.coalesce(ScanRun.finished_at, ScanRun.started_at)).nullslast())
                .limit(1)
            )
            if active is not None:
                return active
            checkpoint: dict[str, Any] = {}
            if resume_from_checkpoint:
                previous = await session.scalar(
                    select(ScanRun)
                    .where(
                        ScanRun.source_id == source_id,
                        ScanRun.scan_type == scan_type,
                        ScanRun.status.in_([RunStatus.FAILED, RunStatus.PARTIAL]),
                    )
                    .order_by(
                        desc(func.coalesce(ScanRun.finished_at, ScanRun.started_at)).nullslast()
                    )
                    .limit(1)
                )
                if previous is not None:
                    checkpoint = previous.checkpoint
            run = ScanRun(
                source_id=source_id,
                scan_type=scan_type,
                status=RunStatus.QUEUED,
                checkpoint=checkpoint,
            )
            session.add(run)
            await session.flush()
            await record_audit_event(
                session,
                actor=actor,
                action="scan.queued",
                entity_type="scan_run",
                entity_id=str(run.id),
                correlation_id=str(run.id),
                details={"source_id": str(source_id), "scan_type": scan_type.value},
            )
            await session.commit()
            return run

    async def _fail_run(
        self, session: AsyncSession, run: ScanRun, source: JobSource, reason: str
    ) -> None:
        run.status = RunStatus.FAILED
        run.finished_at = datetime.now(UTC)
        run.diagnostics = {**run.diagnostics, "failure": reason}
        source.last_scan_status = RunStatus.FAILED
        await record_audit_event(
            session,
            actor="worker",
            action="scan.failed",
            entity_type="scan_run",
            entity_id=str(run.id),
            correlation_id=str(run.id),
            decision="failed",
            details={"reason": reason},
        )
        await session.commit()

    async def run_scan(self, scan_id: UUID) -> ScanRun:
        async with self.session_factory() as session:
            run = await session.get(ScanRun, scan_id)
            if run is None:
                raise LookupError(f"scan {scan_id} does not exist")
            source = await session.get(JobSource, run.source_id)
            if source is None:
                raise LookupError(f"source for scan {scan_id} does not exist")
            if run.status not in {RunStatus.QUEUED, RunStatus.PARTIAL, RunStatus.FAILED}:
                return run
            if not source.enabled:
                await self._fail_run(session, run, source, "source is disabled")
                return run
            if source.health_status == SourceHealth.PAUSED:
                await self._fail_run(session, run, source, "source crawling is paused")
                return run

            recovering_automatic_pause = (
                source.health_status == SourceHealth.DEGRADED and source.automatic_actions_paused
            )
            # Validation/access failures are real scan attempts too. Persist a start time so
            # completed early failures sort correctly against later successful scans.
            run.started_at = run.started_at or datetime.now(UTC)

            adapter = self.registry.create(source)
            try:
                validation = await adapter.validate_source()
            except BaseException:
                await adapter.aclose()
                raise
            if not validation.valid:
                source.health_status = SourceHealth.DEGRADED
                source.automatic_actions_paused = True
                await self._fail_run(
                    session,
                    run,
                    source,
                    "adapter validation failed",
                )
                await adapter.aclose()
                return run
            try:
                access = await adapter.check_access_policy()
            except BaseException:
                await adapter.aclose()
                raise
            if not access.allowed:
                source.health_status = SourceHealth.PAUSED
                source.automatic_actions_paused = True
                await self._fail_run(session, run, source, "access policy denied")
                await adapter.aclose()
                return run

            run.status = RunStatus.RUNNING
            run.started_at = run.started_at or datetime.now(UTC)
            await session.commit()

            try:
                categories = await adapter.discover_categories()
                run.discovered_categories = len(categories)
                await self._save_categories(session, source.id, categories)
                run.scanned_entrypoints = (
                    len(categories)
                    + len(await adapter.discover_regions())
                    + len(await adapter.discover_locales())
                )
                await session.commit()
            except Exception as exc:  # adapter boundaries must turn parse failures into run state
                run.parsing_errors += 1
                run.diagnostics = {"discovery_error": type(exc).__name__}
                await session.commit()

            checkpoint = ScanCheckpoint.model_validate(run.checkpoint or {})
            if run.scan_type == ScanType.INCREMENTAL:
                checkpoint = await self._seed_incremental_known_state(
                    session,
                    source.id,
                    checkpoint,
                )
                run.checkpoint = checkpoint.model_dump(mode="json")
                await session.commit()
            iterator = (
                adapter.iterate_full_scan(checkpoint)
                if run.scan_type == ScanType.FULL
                else adapter.iterate_incremental_scan(checkpoint)
            )
            # IDs already committed by a resumed scan are metadata-only duplicates. This avoids
            # refetching their detail pages while still allowing later category/locale merges.
            processed_ids: set[str] = set(checkpoint.yielded_external_ids)
            observed_pages: set[str] = set()
            forced_degradation_reason: str | None = None
            try:
                async for reference in iterator:
                    if reference.discovery_url and reference.discovery_url not in observed_pages:
                        observed_pages.add(reference.discovery_url)
                        run.scanned_pages += 1
                    if reference.external_id in processed_ids:
                        await self._merge_reference_metadata(session, source.id, reference)
                        await self._save_checkpoint(session, run, reference, succeeded=True)
                        continue
                    processed_ids.add(reference.external_id)
                    run.found_jobs += 1
                    if reference.metadata.get("known_unchanged") is True:
                        await self._merge_reference_metadata(session, source.id, reference)
                        run.unchanged_jobs += 1
                        await self._save_checkpoint(session, run, reference, succeeded=True)
                        await session.commit()
                        continue
                    try:
                        raw = await adapter.fetch_job_details(reference)
                        normalized = await adapter.normalize_job(raw)
                        outcome = await self._upsert_job(session, source, normalized, reference)
                        if outcome == "new":
                            run.new_jobs += 1
                        elif outcome == "updated":
                            run.updated_jobs += 1
                        else:
                            run.unchanged_jobs += 1
                        await self._save_checkpoint(session, run, reference, succeeded=True)
                        await session.commit()
                    except Exception as exc:
                        run.parsing_errors += 1
                        diagnostics = dict(run.diagnostics)
                        errors = list(diagnostics.get("errors", []))
                        errors.append(
                            {
                                "external_id": reference.external_id,
                                "type": type(exc).__name__,
                            }
                        )
                        diagnostics["errors"] = errors[-20:]
                        run.diagnostics = diagnostics
                        failure_count = await self._save_checkpoint(
                            session,
                            run,
                            reference,
                            succeeded=False,
                        )
                        await session.commit()
                        forced_degradation_reason = _degradation_reason(exc)
                        if failure_count >= 3 and forced_degradation_reason is None:
                            forced_degradation_reason = (
                                "the same job detail failed parsing three times"
                            )
                        # Stop at the failed reference. Its ID is deliberately absent
                        # from the checkpoint, so a resumed scan retries it before any
                        # later entrypoint instead of silently losing the vacancy.
                        break
            except Exception as exc:
                run.network_errors += 1
                run.status = RunStatus.PARTIAL
                # Some adapters mutate the supplied checkpoint directly; persist it even when
                # iteration fails before another reference can carry checkpoint metadata.
                run.checkpoint = self._merge_checkpoint_progress(run.checkpoint, checkpoint)
                run.diagnostics = {
                    **run.diagnostics,
                    "iteration_error": type(exc).__name__,
                }
                run.finished_at = datetime.now(UTC)
                source.last_scan_status = RunStatus.PARTIAL
                degradation_reason = _degradation_reason(exc)
                if degradation_reason:
                    source.health_status = SourceHealth.DEGRADED
                    source.automatic_actions_paused = True
                    session.add(
                        Alert(
                            source_id=source.id,
                            severity="high",
                            code="adapter_access_degraded",
                            message=degradation_reason,
                            safe_diagnostics={
                                "scan_id": str(run.id),
                                "error_type": type(exc).__name__,
                            },
                        )
                    )
                await session.commit()
                await adapter.aclose()
                return run

            # Capture completed-entrypoint progress after a clean iterator shutdown as well.
            # Per-reference metadata remains authoritative for adapters using checkpoint copies.
            run.checkpoint = self._merge_checkpoint_progress(run.checkpoint, checkpoint)
            degraded_reason = forced_degradation_reason or await self._detect_degradation(
                session,
                run,
            )
            if degraded_reason:
                source.health_status = SourceHealth.DEGRADED
                source.automatic_actions_paused = True
                session.add(
                    Alert(
                        source_id=source.id,
                        severity="high",
                        code="adapter_degradation",
                        message=degraded_reason,
                        safe_diagnostics={
                            "scan_id": str(run.id),
                            "found_jobs": run.found_jobs,
                            "parsing_errors": run.parsing_errors,
                        },
                    )
                )
                run.status = RunStatus.PARTIAL
            else:
                source.health_status = SourceHealth.HEALTHY
                if recovering_automatic_pause:
                    source.automatic_actions_paused = False
                run.status = RunStatus.SUCCEEDED if run.parsing_errors == 0 else RunStatus.PARTIAL
            source.last_scan_status = run.status
            run.finished_at = datetime.now(UTC)
            await record_audit_event(
                session,
                actor="worker",
                action="scan.finished",
                entity_type="scan_run",
                entity_id=str(run.id),
                correlation_id=str(run.id),
                decision=run.status.value,
                details={
                    "found": run.found_jobs,
                    "new": run.new_jobs,
                    "updated": run.updated_jobs,
                    "unchanged": run.unchanged_jobs,
                    "errors": run.parsing_errors + run.network_errors,
                },
            )
            await session.commit()
            await adapter.aclose()
            return run

    async def _seed_incremental_known_state(
        self,
        session: AsyncSession,
        source_id: UUID,
        checkpoint: ScanCheckpoint,
    ) -> ScanCheckpoint:
        """Give adapters stable listing hints without coupling them to persistence."""

        rows = (
            await session.execute(
                select(
                    SourceJob.external_job_id,
                    SourceJob.raw_metadata,
                    SourceJob.source_updated_at,
                    SourceJob.last_checked_at,
                ).where(SourceJob.source_id == source_id)
            )
        ).all()
        known_ids = {
            value
            for value in checkpoint.adapter_state.get("known_external_ids", [])
            if isinstance(value, str)
        }
        raw_hints = checkpoint.adapter_state.get("known_updated_hints", {})
        known_hints = dict(raw_hints) if isinstance(raw_hints, dict) else {}
        raw_checks = checkpoint.adapter_state.get("known_last_checked_at", {})
        known_checks = dict(raw_checks) if isinstance(raw_checks, dict) else {}
        for external_id, metadata, source_updated_at, last_checked_at in rows:
            known_ids.add(external_id)
            listing_hint = (
                metadata.get("listing_updated_hint") if isinstance(metadata, dict) else None
            )
            if isinstance(listing_hint, str) and listing_hint:
                known_hints[external_id] = listing_hint
            elif source_updated_at is not None:
                known_hints[external_id] = source_updated_at.isoformat()
            if last_checked_at is not None:
                known_checks[external_id] = last_checked_at.isoformat()
        checkpoint.adapter_state = {
            **checkpoint.adapter_state,
            "known_external_ids": sorted(known_ids),
            "known_updated_hints": known_hints,
            "known_last_checked_at": known_checks,
        }
        return checkpoint

    async def _save_checkpoint(
        self,
        session: AsyncSession,
        run: ScanRun,
        reference: RawJobReference,
        *,
        succeeded: bool,
    ) -> int:
        raw_checkpoint = reference.metadata.get("scan_checkpoint")
        failure_count = 0
        if isinstance(raw_checkpoint, dict):
            checkpoint = ScanCheckpoint.model_validate(raw_checkpoint)
            persisted = ScanCheckpoint.model_validate(run.checkpoint or {})
            raw_failures = persisted.adapter_state.get("failed_reference_attempts", {})
            failures = dict(raw_failures) if isinstance(raw_failures, dict) else {}
            if succeeded:
                failures.pop(reference.external_id, None)
            else:
                checkpoint.yielded_external_ids = [
                    value
                    for value in checkpoint.yielded_external_ids
                    if value != reference.external_id
                ]
                previous = failures.get(reference.external_id, 0)
                failure_count = (previous if isinstance(previous, int) else 0) + 1
                failures[reference.external_id] = failure_count
            checkpoint.adapter_state = {
                **persisted.adapter_state,
                **checkpoint.adapter_state,
                "failed_reference_attempts": failures,
            }
            run.checkpoint = checkpoint.model_dump(mode="json")
        await session.flush()
        return failure_count

    @staticmethod
    def _merge_checkpoint_progress(
        persisted_raw: dict[str, Any] | None,
        mutable: ScanCheckpoint,
    ) -> dict[str, Any]:
        """Merge in-place adapter progress without discarding per-reference progress."""
        persisted = ScanCheckpoint.model_validate(persisted_raw or {})
        baseline = ScanCheckpoint()
        if mutable == baseline:
            return persisted.model_dump(mode="json")

        progressed = mutable.model_copy(deep=True)
        progressed.yielded_external_ids = list(
            dict.fromkeys([*persisted.yielded_external_ids, *mutable.yielded_external_ids])
        )
        progressed.completed_entrypoints = list(
            dict.fromkeys([*persisted.completed_entrypoints, *mutable.completed_entrypoints])
        )
        progressed.adapter_state = {**persisted.adapter_state, **mutable.adapter_state}
        # Per-reference failures are maintained by ``_save_checkpoint``. An adapter may
        # still hold the checkpoint object it received when the scan started, so allowing
        # that stale copy to win here would resurrect a failure that a resumed scan has
        # already retried successfully.
        if "failed_reference_attempts" in persisted.adapter_state:
            progressed.adapter_state["failed_reference_attempts"] = persisted.adapter_state[
                "failed_reference_attempts"
            ]
        else:
            progressed.adapter_state.pop("failed_reference_attempts", None)
        if mutable.entrypoint_index < persisted.entrypoint_index:
            progressed.entrypoint_index = persisted.entrypoint_index
            progressed.page_url = persisted.page_url
            progressed.cursor = persisted.cursor
        elif mutable.cursor is None:
            progressed.cursor = persisted.cursor
        return progressed.model_dump(mode="json")

    async def _save_categories(
        self, session: AsyncSession, source_id: UUID, categories: list[Any]
    ) -> None:
        now = datetime.now(UTC)
        for item in categories:
            existing = await session.scalar(
                select(SourceCategory).where(
                    SourceCategory.source_id == source_id,
                    SourceCategory.external_id == item.external_id,
                    SourceCategory.locale == item.locale,
                )
            )
            if existing is None:
                session.add(
                    SourceCategory(
                        source_id=source_id,
                        external_id=item.external_id,
                        name=item.name,
                        url=item.url,
                        locale=item.locale,
                        active=True,
                        last_seen_at=now,
                    )
                )
            else:
                existing.name = item.name
                existing.url = item.url
                existing.active = True
                existing.last_seen_at = now

    async def _merge_reference_metadata(
        self, session: AsyncSession, source_id: UUID, reference: RawJobReference
    ) -> None:
        job = await session.scalar(
            select(SourceJob).where(
                SourceJob.source_id == source_id,
                SourceJob.external_job_id == reference.external_id,
            )
        )
        if job is not None:
            metadata_categories = reference.metadata.get("categories_seen", [])
            categories = {
                item for item in metadata_categories if isinstance(item, str) and item.strip()
            }
            if reference.category:
                categories.add(reference.category)
            job.categories_seen = sorted(set(job.categories_seen) | categories)
            # General listings are intentionally scanned before some category entrypoints.
            # Preserve that discovery breadth while still assigning a useful primary
            # category when the same publication is later observed in a category page.
            if job.category is None and categories:
                job.category = sorted(categories)[0]
            localized_raw = reference.metadata.get("localized_urls", {})
            if isinstance(localized_raw, dict):
                localized = {
                    str(locale): str(url)
                    for locale, url in localized_raw.items()
                    if isinstance(locale, str) and isinstance(url, str)
                }
                job.localized_urls = {**job.localized_urls, **localized}
            job.last_seen_at = datetime.now(UTC)

    async def _upsert_job(
        self,
        session: AsyncSession,
        source: JobSource,
        normalized: NormalizedJobData,
        reference: RawJobReference,
    ) -> str:
        now = datetime.now(UTC)
        existing = await session.scalar(
            select(SourceJob).where(
                SourceJob.source_id == source.id,
                SourceJob.external_job_id == normalized.external_job_id,
            )
        )
        categories = sorted(
            set(normalized.categories_seen)
            | ({reference.category} if reference.category else set())
        )
        raw_metadata = dict(normalized.raw_metadata)
        if reference.updated_hint:
            raw_metadata["listing_updated_hint"] = reference.updated_hint
        if existing is None:
            job = SourceJob(
                source_id=source.id,
                external_job_id=normalized.external_job_id,
                canonical_url=normalized.canonical_url,
                localized_urls=normalized.localized_urls,
                title=normalized.title,
                company=normalized.company,
                employer_url=normalized.employer_url,
                categories_seen=categories,
                category=normalized.category,
                subcategory=normalized.subcategory,
                description=normalized.description,
                requirements=normalized.requirements,
                responsibilities=normalized.responsibilities,
                salary_text=normalized.salary_text,
                salary_min=normalized.salary_min,
                salary_max=normalized.salary_max,
                currency=normalized.currency,
                location=normalized.city,
                cities=normalized.cities,
                schedule=normalized.schedule,
                employment_type=normalized.employment_type,
                required_experience=normalized.required_experience,
                no_experience=normalized.no_experience,
                workplace_type=normalized.workplace_type,
                public_email=normalized.public_email,
                public_phone=normalized.public_phone,
                public_emails=normalized.public_emails,
                public_phones=normalized.public_phones,
                application_url=normalized.application_url,
                page_locale=normalized.page_locale,
                published_at=normalized.published_at,
                source_updated_at=normalized.updated_at,
                first_seen_at=now,
                last_seen_at=now,
                last_checked_at=now,
                content_hash=normalized.content_hash,
                source_fingerprint=normalized.source_fingerprint,
                status=normalized.status,
                raw_metadata=raw_metadata,
            )
            job.matching_content_hash = compute_source_matching_hash(job)
            session.add(job)
            await session.flush()
            result = await self.deduplication.assign(session, job)
            await self._refresh_canonical_status(session, {result.canonical_job.id})
            return "new"

        changed = existing.content_hash != normalized.content_hash
        existing.localized_urls = {**existing.localized_urls, **normalized.localized_urls}
        existing.categories_seen = sorted(set(existing.categories_seen) | set(categories))
        if existing.category is None and categories:
            existing.category = sorted(categories)[0]
        if existing.page_locale is None and normalized.page_locale:
            existing.page_locale = normalized.page_locale
        existing.last_seen_at = now
        existing.last_checked_at = now
        existing.confirmed_absence_count = 0
        existing.raw_metadata = {**existing.raw_metadata, **raw_metadata}
        if normalized.status == JobStatus.ACTIVE and existing.status != JobStatus.ACTIVE:
            existing.status = JobStatus.ACTIVE
        if not changed:
            existing.matching_content_hash = compute_source_matching_hash(existing)
            if existing.canonical_job_id is not None:
                await self._refresh_canonical_status(session, {existing.canonical_job_id})
            await session.flush()
            return "unchanged"

        changed_fields: list[str] = []
        value_map: dict[str, Any] = {
            **normalized.model_dump(),
            "location": normalized.city,
            "cities": normalized.cities,
            "source_updated_at": normalized.updated_at,
            "raw_metadata": existing.raw_metadata,
        }
        for field in SOURCE_JOB_FIELDS:
            # Category and locale describe where the publication was discovered.
            # They are merged above and must not turn a material vacancy update
            # into a false positive when entrypoint order changes.
            if field in {"category", "page_locale"}:
                continue
            if field not in value_map:
                continue
            new_value = value_map[field]
            if getattr(existing, field) != new_value:
                changed_fields.append(field)
                setattr(existing, field, new_value)
        existing.matching_content_hash = compute_source_matching_hash(existing)
        session.add(
            JobSnapshot(
                source_job_id=existing.id,
                changed_fields=changed_fields,
                description=normalized.description,
                salary={
                    "text": normalized.salary_text,
                    "minimum": str(normalized.salary_min)
                    if normalized.salary_min is not None
                    else None,
                    "maximum": str(normalized.salary_max)
                    if normalized.salary_max is not None
                    else None,
                    "currency": normalized.currency,
                },
                requirements=normalized.requirements,
                contacts={
                    "email": normalized.public_email,
                    "phone": normalized.public_phone,
                    "emails": normalized.public_emails,
                    "phones": normalized.public_phones,
                },
                content_hash=normalized.content_hash,
                requires_rematch=changes_require_rematch(changed_fields),
            )
        )
        if existing.canonical_job_id is not None:
            await self._refresh_canonical_status(session, {existing.canonical_job_id})
        await session.flush()
        return "updated"

    async def _detect_degradation(self, session: AsyncSession, run: ScanRun) -> str | None:
        previous = await session.scalar(
            select(ScanRun)
            .where(
                ScanRun.source_id == run.source_id,
                ScanRun.id != run.id,
                ScanRun.scan_type == run.scan_type,
                ScanRun.status == RunStatus.SUCCEEDED,
                ScanRun.found_jobs > 0,
            )
            .order_by(desc(ScanRun.finished_at))
            .limit(1)
        )
        if run.found_jobs == 0:
            return "source returned zero jobs; automatic actions were paused for review"
        if (
            previous is not None
            and previous.found_jobs >= 20
            and run.found_jobs < previous.found_jobs * 0.2
        ):
            return "source result count dropped by more than 80%"
        total_attempts = run.new_jobs + run.updated_jobs + run.unchanged_jobs + run.parsing_errors
        if total_attempts >= 5 and run.parsing_errors / total_attempts > 0.4:
            return "parsing error rate exceeded 40%"
        return None

    async def recheck_active_jobs(
        self, source_id: UUID, close_after_confirmed_absence_count: int = 3
    ) -> dict[str, int]:
        async with self.session_factory() as session:
            source = await session.get(JobSource, source_id)
            if source is None:
                raise LookupError(f"source {source_id} does not exist")
            if not source.enabled or source.health_status in {
                SourceHealth.DEGRADED,
                SourceHealth.PAUSED,
                SourceHealth.DISABLED,
            }:
                return {"checked": 0, "updated": 0, "possibly_closed": 0, "closed": 0, "errors": 0}
            jobs = list(
                (
                    await session.scalars(
                        select(SourceJob).where(
                            SourceJob.source_id == source_id,
                            SourceJob.status.in_([JobStatus.ACTIVE, JobStatus.POSSIBLY_CLOSED]),
                        )
                    )
                ).all()
            )
            adapter = self.registry.create(source)
            results: list[tuple[SourceJob, Any]] = []
            try:
                for job in jobs:
                    results.append((job, await adapter.recheck_job(job)))
            finally:
                await adapter.aclose()
            absences = sum(result.exists is False for _, result in results)
            degraded_results = sum(result.adapter_degraded for _, result in results)
            suspicious_mass_absence = bool(
                results
                and (
                    (len(results) >= 2 and absences == len(results))
                    or (len(results) >= 4 and absences / len(results) > 0.5)
                )
            )
            if degraded_results or suspicious_mass_absence:
                source.health_status = SourceHealth.DEGRADED
                source.automatic_actions_paused = True
                session.add(
                    Alert(
                        source_id=source_id,
                        severity="critical",
                        code="mass_absence_suppressed",
                        message=(
                            "Recheck state changes were suppressed because the adapter "
                            "degraded or a suspicious share of the source disappeared"
                        ),
                        safe_diagnostics={
                            "checked": len(results),
                            "absent": absences,
                            "adapter_degraded": degraded_results,
                        },
                    )
                )
                await session.commit()
                return {
                    "checked": len(results),
                    "updated": 0,
                    "possibly_closed": 0,
                    "closed": 0,
                    "errors": degraded_results,
                }
            counters = {
                "checked": len(results),
                "updated": 0,
                "possibly_closed": 0,
                "closed": 0,
                "errors": 0,
            }
            for job, result in results:
                job.last_checked_at = datetime.now(UTC)
                if result.adapter_degraded:
                    source.health_status = SourceHealth.DEGRADED
                    source.automatic_actions_paused = True
                    counters["errors"] += 1
                    continue
                if result.exists is None:
                    counters["errors"] += 1
                    continue
                if result.explicitly_closed:
                    job.status = JobStatus.CLOSED
                    job.confirmed_absence_count = close_after_confirmed_absence_count
                    counters["closed"] += 1
                    continue
                if result.exists is False:
                    job.confirmed_absence_count += 1
                    if job.confirmed_absence_count >= close_after_confirmed_absence_count:
                        job.status = JobStatus.CLOSED
                        counters["closed"] += 1
                    else:
                        job.status = JobStatus.POSSIBLY_CLOSED
                        counters["possibly_closed"] += 1
                    continue
                job.confirmed_absence_count = 0
                job.status = JobStatus.ACTIVE
                job.last_seen_at = datetime.now(UTC)
                if result.changed and result.normalized_job is not None:
                    await self._upsert_job(
                        session,
                        source,
                        result.normalized_job,
                        RawJobReference(
                            external_id=job.external_job_id,
                            url=job.canonical_url,
                            locale=job.page_locale,
                            category=job.category,
                        ),
                    )
                    counters["updated"] += 1
            await self._refresh_canonical_status(
                session,
                {
                    job.canonical_job_id
                    for job, _result in results
                    if job.canonical_job_id is not None
                },
            )
            await block_closed_vacancy_applications(
                session,
                actor="scan_recheck",
                canonical_job_ids={
                    job.canonical_job_id
                    for job, _result in results
                    if job.canonical_job_id is not None
                },
            )
            await session.commit()
            return counters

    async def create_batch_incremental(self, actor: str = "scheduler") -> BatchScanRun:
        async with self.session_factory() as session:
            source_ids = list(
                (
                    await session.scalars(
                        select(JobSource.id).where(
                            JobSource.enabled.is_(True),
                            JobSource.health_status.notin_(
                                [
                                    SourceHealth.DEGRADED,
                                    SourceHealth.PAUSED,
                                    SourceHealth.DISABLED,
                                ]
                            ),
                        )
                    )
                ).all()
            )
        child_ids: list[str] = []
        for source_id in source_ids:
            run = await self.create_scan(source_id, ScanType.INCREMENTAL, actor=actor)
            child_ids.append(str(run.id))
        async with self.session_factory() as session:
            batch = BatchScanRun(
                child_scan_ids=child_ids,
                status=RunStatus.QUEUED,
                summary={"sources": len(child_ids)},
                started_at=datetime.now(UTC),
            )
            session.add(batch)
            await session.commit()
            return batch


def decimal_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None

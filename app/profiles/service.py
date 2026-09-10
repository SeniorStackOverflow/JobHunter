from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers.parsing.normalization import normalize_for_fingerprint
from app.models.entities import (
    Application,
    JobPreference,
    MatchEvaluation,
    Resume,
    SourceJob,
    UserProfile,
)
from app.profiles.schemas import (
    JobPreferenceInput,
    JobPreferenceUpdateInput,
    ResumeMetadataInput,
    UserProfileInput,
)
from app.security.files import UnsafeResumeError, safe_storage_path, validate_resume_upload
from app.settings import Settings

logger = structlog.get_logger(__name__)

# Every file-transaction marker records the nonce of the ``api`` process that
# wrote it (see ``_PROCESS_NONCE``); ``reconcile_file_transactions`` branches on
# that identity, not on marker age. This grace window now only backstops the two
# residual cases with no usable owner: the ``.tmp`` sweep (a torn ``_write_marker``
# write has no readable owner) and the unsupported multi-``api``-replica
# deployment (a live peer's marker would otherwise look like a dead
# predecessor's). 60s is comfortably longer than any single upload/delete request.
RESUME_TRANSACTION_GRACE_SECONDS = 60

# Regenerated on every import, i.e. once per ``api`` process. Written into every
# transaction marker so a later reconcile can tell "this same live process wrote
# it" (a request may still be in flight -- leave its file alone) from "a
# previous, now-dead process wrote it" (a crashed predecessor -- reconcile it
# immediately). Relies on the single-writer invariant documented on
# ``reconcile_file_transactions``.
_PROCESS_NONCE = uuid4().hex


@dataclass(frozen=True)
class ResumeDeletion:
    resume_id: UUID
    profile_id: UUID
    storage_key: str | None
    original_filename: str
    sha256: str
    name: str
    category: str

    @classmethod
    def from_resume(cls, resume: Resume) -> ResumeDeletion:
        return cls(
            resume_id=resume.id,
            profile_id=resume.profile_id,
            storage_key=None if resume.storage_key.startswith("pending/") else resume.storage_key,
            original_filename=resume.original_filename,
            sha256=resume.sha256,
            name=resume.name,
            category=resume.category,
        )

    def audit_details(self) -> dict[str, str]:
        return {
            "profile_id": str(self.profile_id),
            "sha256": self.sha256,
            "original_filename": self.original_filename,
            "name": self.name,
            "category": self.category,
        }


class ResumeInUseError(RuntimeError):
    """A resume referenced by an application or evaluation cannot be deleted."""


def choose_resume_for_job(resumes: list[Resume], job: SourceJob) -> Resume | None:
    normalized_categories = {
        normalize_for_fingerprint(value)
        for value in (job.category, job.subcategory, *(job.categories_seen or []))
        if value
    }
    exact = next(
        (
            resume
            for resume in resumes
            if normalize_for_fingerprint(resume.category) in normalized_categories
        ),
        None,
    )
    return exact or next(
        (resume for resume in resumes if resume.is_default), resumes[0] if resumes else None
    )


class ProfileService:
    async def list_profiles(self, session: AsyncSession) -> list[UserProfile]:
        return list(
            (
                await session.scalars(
                    select(UserProfile).order_by(UserProfile.created_at, UserProfile.id)
                )
            ).all()
        )

    async def get_profile(
        self, session: AsyncSession, profile_id: UUID | None = None
    ) -> UserProfile | None:
        if profile_id is not None:
            return await session.get(UserProfile, profile_id)
        profile = await session.scalar(
            select(UserProfile).where(UserProfile.is_default.is_(True)).limit(1)
        )
        if profile is not None:
            return profile
        return cast(
            UserProfile | None,
            await session.scalar(
                select(UserProfile).order_by(UserProfile.created_at, UserProfile.id).limit(1)
            ),
        )

    async def create_profile(
        self, session: AsyncSession, payload: UserProfileInput, *, make_default: bool = False
    ) -> UserProfile:
        profiles = await self.list_profiles(session)
        make_default = make_default or not profiles
        if make_default:
            await session.execute(update(UserProfile).values(is_default=False))
        profile = UserProfile(**payload.model_dump(mode="json"), is_default=make_default)
        session.add(profile)
        await session.flush()
        session.add(JobPreference(profile_id=profile.id))
        await session.flush()
        return profile

    async def upsert_profile(
        self, session: AsyncSession, payload: UserProfileInput, profile_id: UUID | None = None
    ) -> UserProfile:
        profile = await self.get_profile(session, profile_id)
        values = payload.model_dump(mode="json")
        if profile is None:
            return await self.create_profile(session, payload, make_default=True)
        for key, value in values.items():
            setattr(profile, key, value)
        await session.flush()
        return profile

    async def set_default_profile(self, session: AsyncSession, profile_id: UUID) -> UserProfile:
        profile = await self.get_profile(session, profile_id)
        if profile is None:
            raise LookupError(f"profile {profile_id} does not exist")
        await session.execute(update(UserProfile).values(is_default=False))
        profile.is_default = True
        await session.flush()
        return profile

    async def get_preferences(
        self, session: AsyncSession, profile_id: UUID | None = None
    ) -> JobPreference:
        profile = await self.get_profile(session, profile_id)
        if profile is None:
            raise LookupError("a user profile is required before preferences")
        preferences = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == profile.id).limit(1)
        )
        if preferences is None:
            preferences = JobPreference(profile_id=profile.id)
            session.add(preferences)
            await session.flush()
        return preferences

    async def upsert_preferences(
        self, session: AsyncSession, payload: JobPreferenceInput, profile_id: UUID | None = None
    ) -> JobPreference:
        preferences = await self.get_preferences(session, profile_id)
        for key, value in payload.model_dump(mode="json").items():
            setattr(preferences, key, value)
        await session.flush()
        return preferences

    async def update_preferences(
        self,
        session: AsyncSession,
        payload: JobPreferenceUpdateInput,
        profile_id: UUID | None = None,
    ) -> JobPreference:
        preferences = await self.get_preferences(session, profile_id)
        for key, value in payload.model_dump(mode="json", exclude_unset=True).items():
            setattr(preferences, key, value)
        await session.flush()
        return preferences

    async def pause_auto_send(
        self, session: AsyncSession, profile_id: UUID | None = None
    ) -> JobPreference:
        preferences = await self.get_preferences(session, profile_id)
        preferences.global_pause = True
        await session.flush()
        return preferences

    async def resume_auto_send(
        self, session: AsyncSession, profile_id: UUID | None = None
    ) -> JobPreference:
        preferences = await self.get_preferences(session, profile_id)
        preferences.auto_send_enabled = True
        preferences.global_pause = False
        await session.flush()
        return preferences


class ResumeService:
    _TRANSACTION_DIR = ".transactions"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Set by ``upload`` right before it writes its marker, so a failing
        # request's ``except`` path can roll back exactly the storage key this
        # call used -- ``validate_resume_upload`` mints a fresh uuid-prefixed
        # name the handler cannot recompute. One ``ResumeService`` is built per
        # request, so this never leaks between requests.
        self._last_upload_key: str | None = None

    def _transaction_dir(self) -> Path:
        # Pure path computation, no side effect: only _write_marker needs the
        # directory to exist, so the cleanup-only finalize_* paths do not recreate it.
        return self.settings.resume_storage_path / self._TRANSACTION_DIR

    def _marker_path(self, operation: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._transaction_dir() / f"{operation}-{digest}.json"

    def _write_marker(self, operation: str, *, key: str, payload: dict[str, str]) -> Path:
        self._transaction_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = self._marker_path(operation, key)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"operation": operation, "owner": _PROCESS_NONCE, **payload}),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(marker)
        return marker

    @staticmethod
    def _remove_marker(marker: Path) -> None:
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "resume_transaction_marker_cleanup_failed",
                marker=marker.name,
                error_type=type(exc).__name__,
            )

    def _unlink_storage_key(self, storage_key: str, *, resume_id: UUID | None = None) -> bool:
        try:
            safe_storage_path(self.settings.resume_storage_path, storage_key).unlink(
                missing_ok=True
            )
        except (UnsafeResumeError, OSError) as exc:
            logger.error(
                "resume_file_cleanup_failed",
                resume_id=str(resume_id) if resume_id else None,
                error_type=type(exc).__name__,
            )
            return False
        return True

    async def reconcile_file_transactions(self, session: AsyncSession) -> None:
        """Sweep orphaned file-transaction markers left by a crashed request.

        Recovers from a crashed process or container restart: markers plus
        ``os.replace`` are atomic against other processes, so a half-finished
        upload or delete is completed or rolled back here on the next start (or
        in a request's ``except`` path). It does NOT protect against host power
        loss -- nothing in this protocol is fsync'd, so a hard crash can lose a
        marker while keeping its file, or keep a marker whose file was already
        written.

        Single-writer assumption: the ``api`` container is the only writer;
        ``worker``/``beat`` mount the volume read-only. Running multiple ``api``
        replicas against one resume volume is unsupported without a
        process-liveness registry.

        Each marker records the ``owner`` nonce of the ``api`` process that wrote
        it, and reconcile branches on that identity rather than on marker age:

        * ``owner == _PROCESS_NONCE`` -- this same live process wrote it, so a
          matching request may still be in flight. Only a marker whose outcome
          is already settled in the database is tidied, and the physical file is
          never touched (this is what makes a slow ``upload`` racing a reconcile
          safe).
        * a foreign or missing ``owner`` -- a previous, now-dead ``api`` process
          wrote it (the single-writer invariant makes it a crashed predecessor;
          a missing ``owner`` is a pre-fix marker, also a predecessor once this
          build is deployed). It is reconciled in full immediately -- unlink the
          file when no row references it, then drop the marker -- with no age
          gate, so a fast container restart recovers the orphan at once.

        ``RESUME_TRANSACTION_GRACE_SECONDS`` no longer gates this ``*.json`` hot
        path; it is now only a fallback for the ``.tmp`` sweep below and for the
        unsupported multi-``api``-replica deployment.
        """
        transaction_dir = self.settings.resume_storage_path / self._TRANSACTION_DIR
        if not transaction_dir.is_dir():
            return
        for marker in transaction_dir.glob("*.json"):
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                operation = str(payload["operation"])
                storage_key = str(payload["storage_key"])
            except FileNotFoundError:
                # A concurrent finalize_* removed the marker between glob and read.
                continue
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.error(
                    "resume_transaction_marker_invalid",
                    marker=marker.name,
                    error_type=type(exc).__name__,
                )
                continue

            # A marker this live process wrote may still have a request behind
            # it: only clear it once its DB outcome is settled, and never unlink
            # its file. A foreign/missing owner is a crashed predecessor -- run
            # the full rollback now.
            own_marker = (
                payload.get("owner") == _PROCESS_NONCE and payload.get("outcome") != "unknown"
            )

            if operation == "upload":
                persisted = await session.scalar(
                    select(Resume.id).where(Resume.storage_key == storage_key).limit(1)
                )
                if own_marker:
                    if persisted is not None:
                        self._remove_marker(marker)
                    continue
                if persisted is not None or self._unlink_storage_key(storage_key):
                    self._remove_marker(marker)
                continue

            if operation == "delete":
                try:
                    resume_id = UUID(str(payload["resume_id"]))
                except (KeyError, ValueError):
                    logger.error("resume_transaction_marker_invalid", marker=marker.name)
                    continue
                persisted = await session.scalar(
                    select(Resume.id).where(Resume.id == resume_id).limit(1)
                )
                if own_marker:
                    if persisted is None:
                        self._remove_marker(marker)
                    continue
                if persisted is not None or self._unlink_storage_key(
                    storage_key, resume_id=resume_id
                ):
                    self._remove_marker(marker)
                continue

            logger.error(
                "resume_transaction_marker_invalid",
                marker=marker.name,
                operation=operation,
            )

        # A crash between ``write_text`` and ``os.replace`` in _write_marker
        # leaves a ``.tmp`` that the ``*.json`` sweep never sees; upload keys are
        # uuid-prefixed so these would otherwise accumulate unbounded. A torn
        # write has no readable owner, so this is the one path still gated on
        # ``RESUME_TRANSACTION_GRACE_SECONDS`` to avoid racing an in-progress
        # ``_write_marker``.
        for stale_tmp in transaction_dir.glob("*.tmp"):
            try:
                age_seconds = time.time() - stale_tmp.stat().st_mtime
            except OSError:
                continue
            if age_seconds < RESUME_TRANSACTION_GRACE_SECONDS:
                continue
            try:
                stale_tmp.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "resume_transaction_tmp_cleanup_failed",
                    marker=stale_tmp.name,
                    error_type=type(exc).__name__,
                )

    def finalize_upload(self, resume: Resume) -> None:
        self._remove_marker(self._marker_path("upload", resume.storage_key))

    def finalize_delete(self, deletion: ResumeDeletion) -> bool:
        if deletion.storage_key is None:
            return True
        marker = self._marker_path("delete", str(deletion.resume_id))
        if not self._unlink_storage_key(deletion.storage_key, resume_id=deletion.resume_id):
            return False
        self._remove_marker(marker)
        return True

    def abort_pending_upload(self, storage_key: str) -> None:
        """Roll back this process's in-flight upload: drop its marker and the file it wrote.

        Safe only for the owning request, whose ``Resume`` row was rolled back.
        """
        self._unlink_storage_key(storage_key)
        self._remove_marker(self._marker_path("upload", storage_key))

    def abort_pending_delete(self, deletion: ResumeDeletion) -> None:
        """Roll back this process's in-flight delete: drop its marker only.

        The ``Resume`` row (and its file) survived the rollback, so the file is
        NOT touched.
        """
        self._remove_marker(self._marker_path("delete", str(deletion.resume_id)))

    async def resolve_upload_commit_outcome(
        self, storage_key: str
    ) -> Literal["committed", "rolled_back", "unknown"]:
        """Resolve an upload after ``commit()`` raised with an unknown outcome.

        The original session is no longer authoritative: PostgreSQL may have
        committed before the client lost the acknowledgement. Mark the file
        transaction as outcome-unknown, then inspect the database through a
        fresh independent session. If verification itself is unavailable the
        marker and file are deliberately left intact for a later reconcile.
        """
        marker = self._marker_path("upload", storage_key)
        try:
            self._write_marker(
                "upload",
                key=storage_key,
                payload={"storage_key": storage_key, "outcome": "unknown"},
            )
        except OSError as exc:
            logger.error(
                "resume_commit_outcome_marker_failed",
                operation="upload",
                error_type=type(exc).__name__,
            )

        try:
            import app.database.session as database_session

            async with database_session.async_session_factory() as verification_session:
                persisted = await verification_session.scalar(
                    select(Resume.id).where(Resume.storage_key == storage_key).limit(1)
                )
        except Exception as exc:
            logger.error(
                "resume_commit_outcome_unresolved",
                operation="upload",
                error_type=type(exc).__name__,
            )
            return "unknown"

        if persisted is not None:
            self._remove_marker(marker)
            return "committed"
        if self._unlink_storage_key(storage_key):
            self._remove_marker(marker)
        return "rolled_back"

    async def resolve_delete_commit_outcome(
        self, deletion: ResumeDeletion
    ) -> Literal["committed", "rolled_back", "unknown"]:
        """Resolve a delete after ``commit()`` raised with an unknown outcome.

        Presence of the resume row in a fresh session means the transaction did
        not commit, so keep the file and drop only the marker. Absence means the
        delete committed, so finish the physical unlink. On verification failure
        leave the marker untouched for later crash/restart reconciliation.
        """
        marker = self._marker_path("delete", str(deletion.resume_id))
        if deletion.storage_key is not None:
            try:
                self._write_marker(
                    "delete",
                    key=str(deletion.resume_id),
                    payload={
                        "resume_id": str(deletion.resume_id),
                        "storage_key": deletion.storage_key,
                        "outcome": "unknown",
                    },
                )
            except OSError as exc:
                logger.error(
                    "resume_commit_outcome_marker_failed",
                    operation="delete",
                    resume_id=str(deletion.resume_id),
                    error_type=type(exc).__name__,
                )

        try:
            import app.database.session as database_session

            async with database_session.async_session_factory() as verification_session:
                persisted = await verification_session.scalar(
                    select(Resume.id).where(Resume.id == deletion.resume_id).limit(1)
                )
        except Exception as exc:
            logger.error(
                "resume_commit_outcome_unresolved",
                operation="delete",
                resume_id=str(deletion.resume_id),
                error_type=type(exc).__name__,
            )
            return "unknown"

        if persisted is not None:
            if deletion.storage_key is not None:
                self._remove_marker(marker)
            return "rolled_back"
        if deletion.storage_key is not None and self._unlink_storage_key(
            deletion.storage_key, resume_id=deletion.resume_id
        ):
            self._remove_marker(marker)
        return "committed"

    async def upload(
        self,
        session: AsyncSession,
        *,
        profile_id: UUID,
        name: str,
        category: str,
        filename: str,
        mime_type: str,
        data: bytes,
        make_default: bool = False,
    ) -> Resume:
        validated = validate_resume_upload(
            filename, mime_type, data, self.settings.max_resume_bytes
        )
        existing = await session.scalar(
            select(Resume).where(Resume.profile_id == profile_id, Resume.sha256 == validated.sha256)
        )
        if existing is not None:
            return existing
        self.settings.resume_storage_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Record the key before the marker exists so a failing request can abort
        # this exact upload even if _write_marker itself is where it fails.
        self._last_upload_key = validated.safe_filename
        self._write_marker(
            "upload",
            key=validated.safe_filename,
            payload={"storage_key": validated.safe_filename},
        )
        destination = safe_storage_path(self.settings.resume_storage_path, validated.safe_filename)
        with destination.open("xb") as handle:
            handle.write(validated.data)
        destination.chmod(0o600)
        if make_default:
            await session.execute(
                update(Resume).where(Resume.profile_id == profile_id).values(is_default=False)
            )
        resume = Resume(
            profile_id=profile_id,
            name=name,
            category=category,
            storage_key=validated.safe_filename,
            original_filename=validated.original_filename,
            mime_type=validated.mime_type,
            sha256=validated.sha256,
            active=True,
            verified=False,
            is_default=make_default,
        )
        session.add(resume)
        await session.flush()
        return resume

    async def register_metadata(
        self, session: AsyncSession, payload: ResumeMetadataInput, profile_id: UUID
    ) -> Resume:
        existing = await session.scalar(
            select(Resume).where(
                Resume.profile_id == profile_id, Resume.sha256 == payload.sha256.lower()
            )
        )
        if existing is not None:
            return existing
        resume = Resume(
            profile_id=profile_id,
            name=payload.name,
            category=payload.category,
            storage_key=f"pending/{uuid4().hex}",
            original_filename=Path(payload.original_filename).name,
            mime_type=payload.mime_type,
            sha256=payload.sha256.lower(),
            active=False,
            verified=False,
            is_default=False,
        )
        session.add(resume)
        await session.flush()
        return resume

    async def activate(self, session: AsyncSession, resume_id: UUID) -> Resume:
        resume = await session.get(Resume, resume_id)
        if resume is None:
            raise LookupError(f"resume {resume_id} does not exist")
        path = safe_storage_path(self.settings.resume_storage_path, resume.storage_key)
        if not path.is_file():
            raise ValueError("resume binary has not been uploaded")
        resume.active = True
        await session.flush()
        return resume

    async def deactivate(self, session: AsyncSession, resume_id: UUID) -> Resume:
        resume = await session.get(Resume, resume_id)
        if resume is None:
            raise LookupError(f"resume {resume_id} does not exist")
        resume.active = False
        resume.is_default = False
        await session.flush()
        return resume

    async def delete(self, session: AsyncSession, resume_id: UUID) -> ResumeDeletion:
        resume = await session.scalar(
            select(Resume)
            .where(Resume.id == resume_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if resume is None:
            raise LookupError(f"resume {resume_id} does not exist")
        application_refs = await session.scalar(
            select(func.count()).select_from(Application).where(Application.resume_id == resume_id)
        )
        evaluation_refs = await session.scalar(
            select(func.count())
            .select_from(MatchEvaluation)
            .where(MatchEvaluation.resume_id == resume_id)
        )
        if application_refs or evaluation_refs:
            raise ResumeInUseError("resume is referenced and can only be deactivated")
        deletion = ResumeDeletion.from_resume(resume)
        if deletion.storage_key is not None:
            self._write_marker(
                "delete",
                key=str(resume_id),
                payload={"resume_id": str(resume_id), "storage_key": deletion.storage_key},
            )
        await session.delete(resume)
        await session.flush()
        return deletion

    async def select_for_category(
        self, session: AsyncSession, profile_id: UUID, category: str | None
    ) -> Resume | None:
        resumes = list(
            (
                await session.scalars(
                    select(Resume).where(
                        Resume.profile_id == profile_id,
                        Resume.active.is_(True),
                        Resume.verified.is_(True),
                    )
                )
            ).all()
        )
        normalized = (category or "").casefold()
        exact = [item for item in resumes if item.category.casefold() == normalized]
        if exact:
            return exact[0]
        return next((item for item in resumes if item.is_default), resumes[0] if resumes else None)

    async def select_for_job(
        self, session: AsyncSession, profile_id: UUID, job: SourceJob
    ) -> Resume | None:
        resumes = list(
            (
                await session.scalars(
                    select(Resume).where(
                        Resume.profile_id == profile_id,
                        Resume.active.is_(True),
                        Resume.verified.is_(True),
                    )
                )
            ).all()
        )
        return choose_resume_for_job(resumes, job)

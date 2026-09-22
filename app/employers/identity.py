from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers.parsing.normalization import normalize_for_fingerprint
from app.models.entities import (
    CanonicalEmployer,
    CanonicalJob,
    EmployerIdentifier,
    EmployerIdentityCandidate,
    JobSource,
    SourceJob,
)
from app.models.enums import EmployerIdentifierType
from app.observability.metrics import EMPLOYER_IDENTITY_AMBIGUOUS, EMPLOYER_IDENTITY_MERGES
from app.phone.numbers import normalize_e164

_FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "mail.ru",
    "outlook.com",
    "yahoo.com",
    "yandex.ru",
}
_RABOTA_EMPLOYER_PATH = re.compile(r"/(?:ru|ro)?/?companies/([^/?#]+)", re.I)
_LEGAL_NAME_TOKENS = {"company", "grup", "group", "sa", "srl", "societate"}


@dataclass(frozen=True)
class IdentitySignal:
    identifier_type: EmployerIdentifierType
    namespace: str
    normalized_value: str
    raw_value: str
    confidence: float
    source: str


@dataclass(frozen=True)
class EmployerIdentityResult:
    employer: CanonicalEmployer
    matched_existing: bool
    ambiguous: bool
    strong_signals: tuple[str, ...]


def _normalized_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        hostname = (parts.hostname or "").casefold().rstrip(".")
        if parts.scheme not in {"http", "https"} or not hostname:
            return None
        port = parts.port
    except ValueError:
        return None
    authority = hostname if port is None else f"{hostname}:{port}"
    path = unquote(parts.path).rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), authority, path, "", ""))


def _email(value: str | None) -> str | None:
    normalized = (value or "").strip().casefold()
    return normalized if normalized.count("@") == 1 else None


def _domain_from_email(value: str | None) -> str | None:
    normalized = _email(value)
    if normalized is None:
        return None
    domain = normalized.rsplit("@", maxsplit=1)[1].rstrip(".")
    return domain if domain and domain not in _FREE_EMAIL_DOMAINS else None


def _domain_matches_company(domain: str, company: str | None) -> bool:
    label = domain.split(".", maxsplit=1)[0]
    company_tokens = [
        token
        for token in normalize_for_fingerprint(company).split()
        if token not in _LEGAL_NAME_TOKENS and len(token) >= 3
    ]
    compact_company = "".join(company_tokens)
    compact_domain = re.sub(r"[^a-z0-9]", "", label.casefold())
    return bool(
        len(compact_domain) >= 4
        and (
            compact_domain in compact_company
            or compact_company in compact_domain
            or compact_domain in company_tokens
        )
    )


def _safe_signal_evidence(signal: IdentitySignal) -> str:
    digest = hashlib.sha256(
        (f"{signal.identifier_type.value}:{signal.namespace}:{signal.normalized_value}").encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()[:16]
    return f"{signal.identifier_type.value}:{digest}"


class EmployerIdentityService:
    """Resolve employers only from exact, durable identifiers.

    Normalized names are retained for display and review, but are deliberately
    excluded from automatic matching.
    """

    async def _signals(self, session: AsyncSession, job: SourceJob) -> list[IdentitySignal]:
        source = await session.get(JobSource, job.source_id)
        namespace = str(job.source_id)
        signals: list[IdentitySignal] = []
        employer_url = _normalized_url(job.employer_url or "") if job.employer_url else None
        contact_namespace = "global"
        if employer_url:
            contact_namespace = f"profile:{namespace}:{employer_url}"
            signals.append(
                IdentitySignal(
                    EmployerIdentifierType.EMPLOYER_PROFILE_URL,
                    namespace,
                    employer_url,
                    job.employer_url or employer_url,
                    1.0,
                    "source_job.employer_url",
                )
            )
            if source is not None and source.adapter_type == "rabota_md":
                match = _RABOTA_EMPLOYER_PATH.search(urlsplit(employer_url).path)
                if match:
                    employer_key = unquote(match.group(1)).strip().casefold()
                    if employer_key:
                        signals.append(
                            IdentitySignal(
                                EmployerIdentifierType.RABOTA_EMPLOYER_ID,
                                namespace,
                                employer_key,
                                match.group(1),
                                1.0,
                                "rabota_employer_profile",
                            )
                        )
        email_values = [job.public_email, *(job.public_emails or [])]
        for raw_email in dict.fromkeys(value for value in email_values if value):
            email = _email(raw_email)
            if email:
                email_domain = email.rsplit("@", maxsplit=1)[1]
                signals.append(
                    IdentitySignal(
                        EmployerIdentifierType.EMAIL,
                        (
                            "global"
                            if _domain_matches_company(email_domain, job.company)
                            else contact_namespace
                        ),
                        email,
                        raw_email or email,
                        0.98,
                        "source_job.public_email",
                    )
                )
            domain = _domain_from_email(raw_email)
            if domain:
                signals.append(
                    IdentitySignal(
                        EmployerIdentifierType.DOMAIN,
                        (
                            "global"
                            if _domain_matches_company(domain, job.company)
                            else contact_namespace
                        ),
                        domain,
                        domain,
                        0.95,
                        "verified_public_email_domain",
                    )
                )
        phone_values = [job.public_phone, *(job.public_phones or [])]
        for raw_phone in dict.fromkeys(value for value in phone_values if value):
            phone = normalize_e164(raw_phone or "", region="MD")
            if phone:
                signals.append(
                    IdentitySignal(
                        EmployerIdentifierType.PHONE,
                        contact_namespace,
                        phone,
                        raw_phone or phone,
                        0.98,
                        "source_job.public_phone",
                    )
                )
        return list(
            {
                (signal.identifier_type, signal.namespace, signal.normalized_value): signal
                for signal in signals
            }.values()
        )

    async def signals_for_source_job(
        self, session: AsyncSession, job: SourceJob
    ) -> tuple[IdentitySignal, ...]:
        """Return normalized evidence without creating or merging employers."""
        return tuple(await self._signals(session, job))

    async def resolve_for_source_job(
        self, session: AsyncSession, job: SourceJob
    ) -> EmployerIdentityResult:
        if job.employer_id is not None:
            employer = await session.get(CanonicalEmployer, job.employer_id)
            if employer is not None:
                return EmployerIdentityResult(employer, True, False, ())

        signals = await self._signals(session, job)
        if signals and session.bind is not None and session.bind.dialect.name == "postgresql":
            lock_keys = sorted(
                f"{signal.identifier_type.value}:{signal.namespace}:{signal.normalized_value}"
                for signal in signals
            )
            for lock_key in lock_keys:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:identity_key))"),
                    {"identity_key": f"jobhunter:employer:{lock_key}"},
                )
        matches: dict[object, CanonicalEmployer] = {}
        for signal in signals:
            identifier = await session.scalar(
                select(EmployerIdentifier).where(
                    EmployerIdentifier.identifier_type == signal.identifier_type,
                    EmployerIdentifier.namespace == signal.namespace,
                    EmployerIdentifier.normalized_value == signal.normalized_value,
                )
            )
            if identifier is not None:
                employer = await session.get(CanonicalEmployer, identifier.employer_id)
                if employer is not None:
                    matches[employer.id] = employer

        normalized_name = normalize_for_fingerprint(job.company)
        ambiguous = len(matches) > 1
        if len(matches) == 1:
            employer = next(iter(matches.values()))
            matched_existing = True
        else:
            primary_domain = next(
                (
                    signal.normalized_value
                    for signal in signals
                    if signal.identifier_type is EmployerIdentifierType.DOMAIN
                ),
                None,
            )
            employer = CanonicalEmployer(
                normalized_name=normalized_name,
                primary_domain=primary_domain,
            )
            session.add(employer)
            await session.flush()
            matched_existing = False

        if not ambiguous:
            for signal in signals:
                existing = await session.scalar(
                    select(EmployerIdentifier.id).where(
                        EmployerIdentifier.identifier_type == signal.identifier_type,
                        EmployerIdentifier.namespace == signal.namespace,
                        EmployerIdentifier.normalized_value == signal.normalized_value,
                    )
                )
                if existing is None:
                    session.add(
                        EmployerIdentifier(
                            employer_id=employer.id,
                            identifier_type=signal.identifier_type,
                            namespace=signal.namespace,
                            normalized_value=signal.normalized_value,
                            raw_value=signal.raw_value,
                            confidence=signal.confidence,
                            source=signal.source,
                        )
                    )
        if normalized_name:
            weak_exists = await session.scalar(
                select(EmployerIdentifier.id).where(
                    EmployerIdentifier.employer_id == employer.id,
                    EmployerIdentifier.identifier_type == EmployerIdentifierType.NORMALIZED_NAME,
                    EmployerIdentifier.normalized_value == normalized_name,
                )
            )
            if weak_exists is None:
                session.add(
                    EmployerIdentifier(
                        employer_id=employer.id,
                        identifier_type=EmployerIdentifierType.NORMALIZED_NAME,
                        namespace="global",
                        normalized_value=normalized_name,
                        raw_value=job.company,
                        confidence=0.4,
                        source="source_job.company",
                    )
                )

        job.employer_id = employer.id
        if job.canonical_job_id is not None:
            canonical = await session.get(CanonicalJob, job.canonical_job_id)
            if canonical is not None and canonical.employer_id is None:
                canonical.employer_id = employer.id
            elif canonical is not None and canonical.employer_id != employer.id:
                ambiguous = True
        if ambiguous:
            candidate = await session.scalar(
                select(EmployerIdentityCandidate).where(
                    EmployerIdentityCandidate.source_job_id == job.id
                )
            )
            payload = sorted(str(value) for value in matches)
            evidence = {"strong_signals": [_safe_signal_evidence(signal) for signal in signals]}
            if candidate is None:
                session.add(
                    EmployerIdentityCandidate(
                        source_job_id=job.id,
                        assigned_employer_id=employer.id,
                        candidate_employer_ids=payload,
                        evidence=evidence,
                    )
                )
            else:
                candidate.assigned_employer_id = employer.id
                candidate.candidate_employer_ids = payload
                candidate.evidence = evidence
                candidate.status = "needs_review"
        await session.flush()
        if ambiguous:
            EMPLOYER_IDENTITY_AMBIGUOUS.inc()
        elif matched_existing:
            EMPLOYER_IDENTITY_MERGES.inc()
        return EmployerIdentityResult(
            employer=employer,
            matched_existing=matched_existing,
            ambiguous=ambiguous,
            strong_signals=tuple(_safe_signal_evidence(signal) for signal in signals),
        )

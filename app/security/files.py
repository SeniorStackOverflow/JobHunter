from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

PDF_MAGIC = b"%PDF-"
SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class UnsafeResumeError(ValueError):
    """Resume did not satisfy the fixed upload policy."""


@dataclass(frozen=True)
class ValidatedResume:
    original_filename: str
    safe_filename: str
    mime_type: str
    sha256: str
    data: bytes


def sanitize_filename(filename: str) -> str:
    basename = Path(filename.replace("\\", "/")).name
    cleaned = SAFE_NAME_PATTERN.sub("_", basename).strip("._")
    if not cleaned:
        cleaned = "resume.pdf"
    return cleaned[:200]


def validate_resume_upload(
    filename: str,
    mime_type: str,
    data: bytes,
    max_bytes: int,
) -> ValidatedResume:
    safe_name = sanitize_filename(filename)
    if Path(safe_name).suffix.lower() != ".pdf":
        raise UnsafeResumeError("only .pdf resumes are accepted")
    if mime_type.lower() != "application/pdf":
        raise UnsafeResumeError("resume MIME type must be application/pdf")
    if not data.startswith(PDF_MAGIC):
        raise UnsafeResumeError("resume does not have a PDF signature")
    if not data or len(data) > max_bytes:
        raise UnsafeResumeError("resume size is outside the configured limit")
    digest = hashlib.sha256(data).hexdigest()
    storage_name = f"{uuid4().hex}-{safe_name}"
    return ValidatedResume(filename, storage_name, mime_type.lower(), digest, data)


def safe_storage_path(root: Path, storage_key: str) -> Path:
    root_resolved = root.resolve()
    candidate = (root_resolved / storage_key).resolve()
    if root_resolved not in candidate.parents:
        raise UnsafeResumeError("storage key escapes the resume directory")
    return candidate


def read_verified_resume(
    root: Path,
    storage_key: str,
    *,
    expected_sha256: str,
    expected_mime_type: str,
    max_bytes: int,
) -> bytes:
    """Bounded, no-follow read with a final PDF and digest check before delivery."""
    if expected_mime_type.casefold() != "application/pdf":
        raise UnsafeResumeError("stored resume MIME type is not application/pdf")
    path = safe_storage_path(root, storage_key)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UnsafeResumeError("verified resume file cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeResumeError("verified resume is not a regular file")
        if metadata.st_size <= 0 or metadata.st_size > max_bytes:
            raise UnsafeResumeError("stored resume size is outside the configured limit")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)

    data = b"".join(chunks)
    if not data.startswith(PDF_MAGIC):
        raise UnsafeResumeError("stored resume no longer has a PDF signature")
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256.casefold()):
        raise UnsafeResumeError("stored resume digest no longer matches verified metadata")
    return data

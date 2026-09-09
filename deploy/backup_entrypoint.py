#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from urllib.parse import parse_qs, unquote, urlsplit

BACKUP_EXECUTABLE = "/usr/local/bin/job-agent-backup"


def parse_migrator_database_url(raw: str) -> dict[str, str]:
    """Convert a SQLAlchemy PostgreSQL URL to explicit, secret-safe libpq inputs."""
    if raw.startswith("postgresql+asyncpg://"):
        raw = "postgresql://" + raw.removeprefix("postgresql+asyncpg://")
    parts = urlsplit(raw)
    if parts.scheme not in {"postgres", "postgresql"}:
        raise ValueError("MIGRATOR_DATABASE_URL must use a PostgreSQL URL scheme")
    if parts.fragment:
        raise ValueError("MIGRATOR_DATABASE_URL must not contain a fragment")
    try:
        port = parts.port or 5432
    except ValueError as exc:
        raise ValueError("MIGRATOR_DATABASE_URL has an invalid port") from exc

    values = {
        "POSTGRES_HOST": parts.hostname or "",
        "POSTGRES_PORT": str(port),
        "POSTGRES_USER": unquote(parts.username or ""),
        "POSTGRES_PASSWORD": unquote(parts.password or ""),
        "POSTGRES_DB": unquote(parts.path.removeprefix("/")),
    }
    labels = {
        "POSTGRES_HOST": "host",
        "POSTGRES_PORT": "port",
        "POSTGRES_USER": "user",
        "POSTGRES_PASSWORD": "password",
        "POSTGRES_DB": "database name",
    }
    for key, value in values.items():
        if not value:
            raise ValueError(f"MIGRATOR_DATABASE_URL is missing {labels[key]}")
        if any(character in value for character in "\r\n\x00"):
            raise ValueError(f"MIGRATOR_DATABASE_URL has an invalid {labels[key]}")

    query = parse_qs(parts.query, keep_blank_values=True)
    unsupported = set(query) - {"sslmode"}
    if unsupported:
        raise ValueError("MIGRATOR_DATABASE_URL has unsupported query options")
    if "sslmode" in query:
        modes = query["sslmode"]
        allowed = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
        if len(modes) != 1 or modes[0] not in allowed:
            raise ValueError("MIGRATOR_DATABASE_URL has an invalid sslmode")
        values["PGSSLMODE"] = modes[0]
    return values


def build_backup_environment(source: Mapping[str, str]) -> dict[str, str]:
    raw = source.get("MIGRATOR_DATABASE_URL", "")
    values = parse_migrator_database_url(raw)
    environment = dict(source)
    environment.pop("MIGRATOR_DATABASE_URL", None)
    for key in ("PGDATABASE", "PGHOST", "PGPASSWORD", "PGPORT", "PGUSER", "PGSSLMODE"):
        environment.pop(key, None)
    environment.update(values)
    return environment


def main() -> int:
    try:
        environment = build_backup_environment(os.environ)
    except ValueError as exc:
        print(f"Backup configuration error: {exc}", file=sys.stderr)
        return 64
    os.execve(  # noqa: S606 - fixed root-owned executable, no shell or user path
        BACKUP_EXECUTABLE, [BACKUP_EXECUTABLE], environment
    )
    return 70


if __name__ == "__main__":
    raise SystemExit(main())

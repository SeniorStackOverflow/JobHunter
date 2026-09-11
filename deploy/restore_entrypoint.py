#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_entrypoint import parse_migrator_database_url

RESTORE_EXECUTABLE = "/usr/local/bin/job-agent-restore"


def build_restore_environment(source: Mapping[str, str]) -> dict[str, str]:
    raw = source.get("MIGRATOR_DATABASE_URL", "")
    values = parse_migrator_database_url(raw)
    environment = dict(source)
    environment.pop("MIGRATOR_DATABASE_URL", None)
    environment.pop("DATABASE_URL", None)
    for key in ("PGDATABASE", "PGHOST", "PGPASSWORD", "PGPORT", "PGUSER", "PGSSLMODE"):
        environment.pop(key, None)
    environment.update(values)
    return environment


def main() -> int:
    try:
        environment = build_restore_environment(os.environ)
    except ValueError as exc:
        print(f"Restore configuration error: {exc}", file=sys.stderr)
        return 64
    os.execve(  # noqa: S606 - fixed root-owned executable, no shell or user path
        RESTORE_EXECUTABLE, [RESTORE_EXECUTABLE], environment
    )
    return 70


if __name__ == "__main__":
    raise SystemExit(main())

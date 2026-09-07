#!/bin/sh
set -eu
if [ -n "${MIGRATOR_DATABASE_URL:-}" ]; then
  export DATABASE_URL="$MIGRATOR_DATABASE_URL"
fi
if [ "${JOBHUNTER_RUNTIME_PREFLIGHT:-0}" = "1" ]; then
  python -m app.database.runtime_preflight
fi
exec "$@"

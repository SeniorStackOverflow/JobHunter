#!/bin/sh
set -eu
cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
exec uv run --no-sync python deploy/dev_a51.py "$@"

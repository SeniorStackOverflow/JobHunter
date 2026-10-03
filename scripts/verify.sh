#!/bin/sh
set -eu

uv sync --extra dev --extra playwright --frozen
uv run --no-sync playwright install chromium

export RUN_PLAYWRIGHT_TESTS=1
export ENABLE_LIVE_RABOTA_SMOKE_TEST=false
export ENABLE_LIVE_DELUCRU_SMOKE_TEST=false
export ENABLE_LIVE_PHONEGATE_SMOKE_TEST=false
export ENABLE_REALCALL_TESTS=false

uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy app fixture_site
uv run --no-sync pytest

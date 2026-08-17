#!/bin/sh
set -eu

uv run ruff format --check .
uv run ruff check .
uv run mypy app fixture_site
uv run pytest


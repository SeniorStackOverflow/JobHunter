#!/bin/sh
set -eu

# The fixture pipeline uses only local HTML, SQLite, the mock LLM and fake Gmail.
uv run pytest tests/e2e/test_autonomous_pipeline.py -q


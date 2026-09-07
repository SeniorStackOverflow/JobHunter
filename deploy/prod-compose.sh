#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
HEAD=$(git rev-parse --verify HEAD)
SHORT_HEAD=$(printf '%s' "$HEAD" | cut -c1-12)
if [ -n "${JOBHUNTER_IMAGE_TAG:-}" ] && [ "$JOBHUNTER_IMAGE_TAG" != "$SHORT_HEAD" ]; then
  echo "JOBHUNTER_IMAGE_TAG=$JOBHUNTER_IMAGE_TAG does not match PROD HEAD $SHORT_HEAD" >&2
  exit 64
fi
export JOBHUNTER_IMAGE_TAG=$SHORT_HEAD
exec docker compose -f docker-compose.yml -f docker-compose.prod.yml "$@"

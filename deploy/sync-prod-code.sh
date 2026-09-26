#!/bin/sh
set -eu

DEV_REPO=${JOBHUNTER_DEV_REPO:-/home/andrei/JobHunter}
PROD_ROOT=${JOBHUNTER_PROD_REPO:-/srv/jobhunter-prod}
DEV_REF=${JOBHUNTER_DEV_REF:-refs/heads/main}

cd "$PROD_ROOT"

if [ "$(pwd -P)" != "$PROD_ROOT" ]; then
  echo "Production code sync may only run from $PROD_ROOT" >&2
  exit 64
fi

if [ "$(git symbolic-ref --short -q HEAD || true)" != "main" ]; then
  echo "Production code sync requires PROD branch main" >&2
  exit 64
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Production working tree is not clean; refusing code sync" >&2
  exit 64
fi

DEV_SHA=$(git -C "$DEV_REPO" rev-parse --verify "$DEV_REF^{commit}")
CURRENT_SHA=$(git rev-parse --verify HEAD)

if [ "$CURRENT_SHA" = "$DEV_SHA" ]; then
  printf 'state=already_synced\nsha=%s\n' "$CURRENT_SHA"
  exit 0
fi

git fetch --no-tags "$DEV_REPO" "$DEV_REF"
TARGET_SHA=$(git rev-parse --verify FETCH_HEAD)

if [ "$TARGET_SHA" != "$DEV_SHA" ]; then
  echo "DEV main moved during fetch; refusing ambiguous deployment" >&2
  exit 65
fi

if ! git merge-base --is-ancestor "$CURRENT_SHA" "$TARGET_SHA"; then
  echo "PROD is not an ancestor of DEV main; refusing non-fast-forward deployment" >&2
  exit 66
fi

git merge --ff-only "$TARGET_SHA"
printf 'state=fast_forwarded\nfrom=%s\nto=%s\n' "$CURRENT_SHA" "$TARGET_SHA"

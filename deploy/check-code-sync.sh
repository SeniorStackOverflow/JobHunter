#!/bin/sh
set -u

DEV_REPO=${JOBHUNTER_DEV_REPO:-/home/andrei/JobHunter}
PROD_REPO=${JOBHUNTER_PROD_REPO:-/srv/jobhunter-prod}
DEV_REF=${JOBHUNTER_DEV_REF:-refs/heads/main}
PROD_REF=${JOBHUNTER_PROD_REF:-HEAD}

emit() {
  printf 'state=%s\n' "$1"
  printf 'dev_ref=%s\n' "$DEV_REF"
  printf 'prod_ref=%s\n' "$PROD_REF"
  [ -n "${DEV_SHA:-}" ] && printf 'dev_sha=%s\n' "$DEV_SHA"
  [ -n "${PROD_SHA:-}" ] && printf 'prod_sha=%s\n' "$PROD_SHA"
  [ -n "${DETAIL:-}" ] && printf 'detail=%s\n' "$DETAIL"
}

unknown() {
  DETAIL=$1
  emit unknown
  exit 0
}

git -C "$DEV_REPO" rev-parse --git-dir >/dev/null 2>&1 || unknown dev_repo_unavailable
git -C "$PROD_REPO" rev-parse --git-dir >/dev/null 2>&1 || unknown prod_repo_unavailable

DEV_SHA=$(git -C "$DEV_REPO" rev-parse --verify "$DEV_REF^{commit}" 2>/dev/null) || unknown dev_ref_unavailable
PROD_SHA=$(git -C "$PROD_REPO" rev-parse --verify "$PROD_REF^{commit}" 2>/dev/null) || unknown prod_ref_unavailable

if [ "$DEV_SHA" = "$PROD_SHA" ]; then
  DETAIL=exact_match
  emit equal
  exit 0
fi

git -C "$DEV_REPO" cat-file -e "$PROD_SHA^{commit}" 2>/dev/null || unknown prod_commit_missing_in_dev_object_db

if git -C "$DEV_REPO" merge-base --is-ancestor "$PROD_SHA" "$DEV_SHA" >/dev/null 2>&1; then
  DETAIL=prod_is_ancestor_of_dev_main
  emit dev_ahead
  exit 0
fi

if git -C "$DEV_REPO" merge-base --is-ancestor "$DEV_SHA" "$PROD_SHA" >/dev/null 2>&1; then
  DETAIL=dev_main_is_ancestor_of_prod
  emit dev_behind
  exit 2
fi

DETAIL=neither_commit_is_ancestor
emit diverged
exit 2

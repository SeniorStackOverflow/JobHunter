FROM postgres:16-alpine

ARG APP_REVISION=dev
LABEL org.opencontainers.image.revision=$APP_REVISION \
      org.opencontainers.image.jobhunter-flavor=production-backup

RUN apk add --no-cache python3

COPY --chmod=0555 deploy/backup.sh /usr/local/bin/job-agent-backup
COPY --chmod=0555 deploy/backup_entrypoint.py /usr/local/bin/job-agent-backup-entrypoint

ENTRYPOINT ["python3", "/usr/local/bin/job-agent-backup-entrypoint"]

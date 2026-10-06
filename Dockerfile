# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /usr/local/bin/uv

ARG COMPILE_BYTECODE=1
ENV UV_COMPILE_BYTECODE=$COMPILE_BYTECODE \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
ARG INSTALL_PLAYWRIGHT=0
RUN if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
        uv sync --frozen --no-dev --extra playwright --no-install-project; \
    else \
        uv sync --frozen --no-dev --no-install-project; \
    fi


FROM python:3.12-slim-bookworm AS runtime

ARG INSTALL_PLAYWRIGHT=0
ARG APP_UID=10001
ARG APP_GID=10001

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    RESUME_STORAGE_PATH=/data/resumes

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid "$APP_GID" jobagent \
    && useradd --system --uid "$APP_UID" --gid jobagent --home-dir /srv/job-agent jobagent \
    && install -d -o jobagent -g jobagent -m 0750 /srv/job-agent /data/resumes /data/phone_evidence

COPY --from=builder /opt/venv /opt/venv

RUN if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
        /opt/venv/bin/playwright install --with-deps chromium \
        && chmod -R a+rX /ms-playwright; \
    fi \
    && printf '#!/bin/sh\nexec python -m app.cli "$@"\n' > /usr/local/bin/job-agent \
    && chmod 0555 /usr/local/bin/job-agent

ARG APP_REVISION=dev
ARG APP_BUILD_FLAVOR=development
ARG JOBHUNTER_DEV_PROJECT=""
ENV APP_REVISION=$APP_REVISION \
    APP_BUILD_FLAVOR=$APP_BUILD_FLAVOR

LABEL org.opencontainers.image.revision=$APP_REVISION \
      org.opencontainers.image.jobhunter-flavor=$APP_BUILD_FLAVOR \
      org.opencontainers.image.jobhunter-project=$JOBHUNTER_DEV_PROJECT \
      org.opencontainers.image.jobhunter-playwright=$INSTALL_PLAYWRIGHT

WORKDIR /srv/job-agent
COPY --chown=jobagent:jobagent app ./app
COPY --chown=jobagent:jobagent fixture_site ./fixture_site
COPY --chown=jobagent:jobagent config ./config
COPY --chown=jobagent:jobagent migrations ./migrations
COPY --chown=jobagent:jobagent alembic.ini pyproject.toml ./
COPY --chown=jobagent:jobagent --chmod=0555 deploy/container-entrypoint.sh ./deploy/container-entrypoint.sh

USER jobagent
EXPOSE 8000
STOPSIGNAL SIGTERM
ENTRYPOINT ["/srv/job-agent/deploy/container-entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

FROM builder AS validation-dependencies
RUN uv sync --frozen --extra dev --no-install-project

FROM runtime AS validation
COPY --from=validation-dependencies /opt/venv /opt/venv
COPY --chown=jobagent:jobagent tests ./tests
COPY --chown=jobagent:jobagent deploy/dev_a51.py ./deploy/dev_a51.py
ENV ENVIRONMENT=test \
    DATABASE_URL=sqlite+aiosqlite:///:memory: \
    EMAIL_PROVIDER=fake \
    REAL_EMAIL_DELIVERY_ENABLED=false \
    LLM_PROVIDER=mock \
    ENABLE_REALCALL_TESTS=false \
    ENABLE_LIVE_RABOTA_SMOKE_TEST=false \
    ENABLE_LIVE_DELUCRU_SMOKE_TEST=false \
    ENABLE_LIVE_PHONEGATE_SMOKE_TEST=false
CMD ["python", "-m", "pytest", "-q"]

# Keep the default build identical to the application runtime; validation is opt-in.
FROM runtime AS production

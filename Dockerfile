# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra playwright --no-install-project

COPY app ./app
COPY fixture_site ./fixture_site
RUN uv sync --frozen --no-dev --extra playwright --no-editable


FROM python:3.12-slim-bookworm AS runtime

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
    && groupadd --system --gid 10001 jobagent \
    && useradd --system --uid 10001 --gid jobagent --home-dir /srv/job-agent jobagent \
    && install -d -o jobagent -g jobagent -m 0750 /srv/job-agent /data/resumes

COPY --from=builder /opt/venv /opt/venv

RUN /opt/venv/bin/playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

WORKDIR /srv/job-agent
COPY --chown=jobagent:jobagent app ./app
COPY --chown=jobagent:jobagent fixture_site ./fixture_site
COPY --chown=jobagent:jobagent config ./config
COPY --chown=jobagent:jobagent migrations ./migrations
COPY --chown=jobagent:jobagent alembic.ini pyproject.toml ./

USER jobagent
EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

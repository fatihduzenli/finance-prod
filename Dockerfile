# syntax=docker/dockerfile:1

# Delivery image for the web process (PLAN-002 Batch B).
#
# Stage 1 resolves the locked dependency set with uv and renders the static
# files; stage 2 is a slim runtime that carries only /app and runs as a
# non-root user. No secret is read, written or baked in at build time
# (root CLAUDE.md 5.2, 5.3): the collectstatic step below uses throwaway
# placeholder values that are passed to a single RUN command and are never
# persisted as image metadata.

# ---------------------------------------------------------------------------
# Stage 1 — builder
# ---------------------------------------------------------------------------
# The uv image tag is pinned and verified to exist on ghcr.io (manifest 200 on
# 2026-09-27). uv 0.12 is required, not merely preferred: uv.lock is a
# `revision = 3` lock file written by uv 0.12, and an older uv refuses a lock
# revision it does not know, so `uv sync --frozen` would fail. From uv 0.10
# onwards the Debian images are Debian 13 (trixie) rather than bookworm, so this
# stage and the runtime stage below must both be trixie: compiled wheels
# (psycopg) link against the builder image's glibc, and the virtual environment
# is copied wholesale between the two stages.
FROM ghcr.io/astral-sh/uv:0.12.19-python3.12-trixie-slim AS builder

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so a source-only change does not reinstall them. The
# repository itself is not a package (`package = false` in pyproject.toml), so
# this installs the locked dependency set and nothing else.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY backend/ ./backend/

# Static files are produced at build time so the runtime image does not need
# write access to the source tree. The environment below is placeholder data
# required by the boot-time env guard; the sqlite URL is never connected to and
# DJANGO_SECRET_KEY=build-only is a literal placeholder, not a credential.
RUN APP_ENV=local \
    STAX_ENV=sandbox \
    STAX_API_BASE_URL=https://apiprod.fattlabs.com \
    DATABASE_URL=sqlite:///tmp/build.db \
    APP_BASE_URL=http://localhost:8000 \
    DJANGO_SECRET_KEY=build-only \
    /app/.venv/bin/python backend/manage.py collectstatic --noinput

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app

COPY --from=builder --chown=app:app /app /app

WORKDIR /app
USER app

EXPOSE 8000

# Django validates the Host header against ALLOWED_HOSTS, which is derived from
# APP_BASE_URL, so the probe sends that host explicitly instead of 127.0.0.1.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, sys, urllib.parse, urllib.request; host = urllib.parse.urlsplit(os.environ['APP_BASE_URL']).netloc; request = urllib.request.Request('http://127.0.0.1:8000/healthz', headers={'Host': host}); sys.exit(0 if urllib.request.urlopen(request, timeout=4).status == 200 else 1)"

# Start-up: apply pending migrations first (backend/finance/management/commands/
# deploy_migrate.py; it takes a PostgreSQL advisory lock, so two replicas starting
# together migrate once). Only if it exits 0 does the shell `exec` gunicorn, which
# then replaces the shell as PID 1 and receives SIGTERM directly. A failed migration
# exits non-zero, gunicorn never starts, and the revision never becomes ready.
# Then provider_environment_check (PLAN-006 D2) asks Stax once, GET /self, whether
# the key's environment matches STAX_ENV and its merchant STAX_EXPECTED_MERCHANT_ID.
# Any disagreement, and an unreachable Stax, exits non-zero: the new revision does
# not start (the running one keeps serving). Locally without a key it is skipped.
# gunicorn runs one worker process with threads. The web makes no Stax call: it runs
# with STAX_CALLER=none, and the worker container app (PLAN-010 D4) is the single
# process that calls Stax and owns the rate limiter and breaker.
#
# Access logging is deliberately left off: inbound webhook URLs carry the
# secret path token (docs/CLAUDE.md Rule 4) and must not reach logs
# (root CLAUDE.md 5.3). Request logging belongs in Django's logging config,
# which redacts the path.
CMD ["/bin/sh", "-c", "python backend/manage.py deploy_migrate && python backend/manage.py provider_environment_check && exec gunicorn finance.wsgi:application --chdir backend --bind 0.0.0.0:8000 --workers 1 --worker-class gthread --threads 4 --timeout 60 --graceful-timeout 30 --error-logfile -"]

# PLAN-003a — Staging defects 1 and 2: migrations on start, error logging

**Date:** 2026-09-29 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Origin:** `docs/STATUS.md` defects 1 and 2. Owner instruction of 2026-09-29: "fix defects 1 and 2".

## 1. Restated objective

On staging the login page returns 500 because the database has no tables: the image
starts gunicorn and nothing ever runs the 27 migrations. The same 500 leaves nothing in
the container log, because Django has no logging configuration and Sentry is off. After
this change the web container applies pending migrations before it serves, safely when
two replicas start together, and every unhandled error and every WARNING or above is
written to stderr with secrets removed.

## 2. Affected packages

Single package: the repository root project (`pyproject.toml`, uv). Touched: `backend/finance/`,
`Dockerfile`, `backend/tests/`, `README.md`. **Out of scope:** `infra/` (Bicep), `.github/`,
`accounts/` behaviour, dependencies (none may be added), the database password rotation
(defect 3), any Stax code.

## 3. Interface contract

**Management command** `deploy_migrate` in `backend/finance/management/commands/deploy_migrate.py`
(`finance` must be added to `INSTALLED_APPS` for command discovery; it has no models).

- No arguments. Output: one line on start, one on success. Exit 0 on success.
- PostgreSQL: takes a session advisory lock with a fixed integer key before migrating, by
  polling `pg_try_advisory_lock`, and releases it in `finally`. Waits at most
  `DEPLOY_MIGRATE_LOCK_TIMEOUT_SECONDS = 90` (module constant). On timeout raises
  `CommandError` with a value-free message; exit status non-zero.
- Other database vendors (sqlite, local and tests): migrates without a lock.
- Runs `migrate` non-interactively for all apps. Never runs `makemigrations`, `flush` or any
  destructive command. A migration failure propagates as a non-zero exit.
- SQL is a constant string with the key passed as a parameter.

**Image start** (`Dockerfile` `CMD`): run `python backend/manage.py deploy_migrate`, and only
if it exits 0, `exec` gunicorn with exactly the present arguments. Gunicorn must end up as
PID 1's replacement so it receives SIGTERM. Access logging stays off. The HEALTHCHECK is unchanged.

**Logging** (`backend/finance/logging.py` plus `LOGGING` in `settings.py`):

- One handler: `logging.StreamHandler` on stderr. One line format:
  `%(asctime)s %(levelname)s %(name)s %(message)s`, followed by the traceback when present.
- Root logger level WARNING. `django.request` errors (unhandled exceptions, 5xx) are written
  with their traceback. Each event is written **once** (no duplicate through Django's default
  `django` logger handlers; no `mail_admins` handler active).
- `RedactingFormatter(logging.Formatter)`: after normal formatting, including the traceback
  text, replaces every occurrence of each configured secret value with `[REDACTED]`. Secret
  values: `STAX_API_KEY`, `STAX_WEBHOOK_URL_TOKEN`, `DJANGO_SECRET_KEY`, the password component
  of `DATABASE_URL`, the password component of `REDIS_URL`, and their URL-quoted forms.
  Empty values and values shorter than 8 characters are skipped, so an unset secret cannot
  turn the log into `[REDACTED]` noise.
- The formatter receives the values from settings at configuration time. It never logs,
  prints or exposes them, and its `repr` must not contain them.
- Same configuration in every `APP_ENV`. No new environment variable.

## 4. Work breakdown (one batch, no file is edited by two tasks)

| Task | Files | Done when |
|---|---|---|
| T1 | `backend/finance/management/__init__.py`, `backend/finance/management/commands/__init__.py`, `backend/finance/management/commands/deploy_migrate.py`, `backend/finance/settings.py` (`INSTALLED_APPS`) | command exists, contract above holds |
| T2 | `backend/finance/logging.py`, `backend/finance/settings.py` (`LOGGING`) | contract above holds |
| T3 | `Dockerfile` (`CMD` and its comment only) | image starts as specified |
| T4 | `backend/tests/test_deploy_migrate.py`, `backend/tests/test_logging.py` | the Opus rows of §5 pass |
| T5 | `README.md`: commands table row for `deploy_migrate`, two sentences on logging, and correct the stale Status section | text matches behaviour |

## 5. Test plan

| # | Case | Author |
|---|---|---|
| 1 | `deploy_migrate` on an empty database applies all migrations; `showmigrations` has none unapplied | Opus |
| 2 | Second run is a no-op and exits 0 | Opus |
| 3 | PostgreSQL path: lock is taken before `migrate` and released after, also when `migrate` raises (assert on the executed SQL or on `pg_locks`) | Opus |
| 4 | Lock held by another session: command fails with `CommandError` after the timeout (timeout patched to about 1 s), message contains no connection detail | Opus |
| 5 | A view that raises produces exactly one log event on stderr with the traceback | Opus |
| 6 | Secret values in the message, in `args` and in the traceback text come out as `[REDACTED]` | Opus |
| 7 | Empty and short secret values do not redact anything | Opus |
| 8 | Webhook-shaped path containing the URL token in a `django.request` error line is redacted | Fable |
| 9 | URL-quoted password and a secret split across `%s` arguments | Fable |
| 10 | Secret inside a chained exception (`raise … from …`) and inside `exc_info` of a different logger | Fable |
| 11 | Two concurrent `deploy_migrate` processes against one PostgreSQL database: both exit 0, migrations applied once | Fable |
| 12 | `Dockerfile` `CMD`: gunicorn is not started when `deploy_migrate` exits non-zero; `exec` is used; no access log flag | Fable (static assertion on the file) |
| 13 | Existing 208 tests still pass; route registry test still passes with `finance` installed | Fable |

PostgreSQL-only tests are skipped when the test database is sqlite and must run in CI, which
uses PostgreSQL 16. Locally they run against `docker-compose.yml` PostgreSQL when it is up.

## 6. Risk register

| Risk | Treatment |
|---|---|
| Secret reaches the container log (webhook token in a request path, database URL in a driver error) | Redacting formatter on the only handler; Fable tests 8 to 10 |
| Two replicas migrate at once (`maxReplicas` is 2) | Advisory lock; test 11 |
| A failed migration leaves the app serving on a half-migrated schema | gunicorn does not start; the revision never becomes ready; the previous revision keeps serving |
| A long migration outlasts the liveness probe (kills after about 120 s) | Not reachable with today's 27 small migrations. Recorded as residual risk; a startup probe is an `infra/` change for a later plan |
| Migrations are irreversible on shared data | Staging holds no data. For production this start-up behaviour is reviewed again at Step 16 |
| Shadowing the standard library by naming a module `logging` | The package is imported as `finance.logging` with `from __future__ import annotations` and absolute imports; Opus must confirm `import logging` inside it resolves to the standard library, or name the module `finance/log_redaction.py` instead and say so |
| Future Celery worker would also migrate | Only the image's default `CMD` migrates; a worker overrides the command |

## 7. Acceptance criteria

1. `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` exit 0.
2. `uv run pytest -m "not e2e" -q` and `uv run pytest -m e2e -q` exit 0 with no test removed, skipped or weakened.
3. Every row of §5 has a passing test.
4. `git diff --stat` touches only the files named in §4.
5. No new dependency; `pyproject.toml` and `uv.lock` unchanged.
6. No secret value, real hostname or real identifier in any new file.
7. After deployment: `GET https://staging.monvera.io/account/login/` returns 200, and a forced error appears in the container log. Verified by Fable after an approved push.

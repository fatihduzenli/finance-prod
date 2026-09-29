## QA Report — PLAN-003a: migrations on start, error logging

**Date:** 2026-09-29 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-003a-staging-defects.md`

**Scope:** root project only.
Production (Opus): `backend/finance/log_redaction.py`, `backend/finance/management/` (three files),
`backend/finance/settings.py`, `Dockerfile` (`CMD` and comment), `README.md`.
Tests (Opus): `backend/tests/test_deploy_migrate.py`, `backend/tests/test_logging.py`.
Tests (Fable): `backend/tests/test_staging_defects_adversarial.py`.
Unchanged, verified with `git diff --quiet`: `pyproject.toml`, `uv.lock`, `infra/`, `.github/`, `backend/accounts/`.

**Commands run** (repository root, placeholder environment, by Fable):

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .` | n/a | 0 | clean |
| `uv run ruff format --check .` | n/a | 0 | 78 files formatted |
| `uv run mypy` (strict) | n/a | 0 | 55 source files |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 236 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 232 passed, 4 skipped (PostgreSQL-only) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 2 passed |
| new test files, three consecutive runs | PostgreSQL 16 | 0 | 30 passed each time, no flake |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | no credential in served files |
| `docker build .` then run against a local PostgreSQL | PostgreSQL 16 | 0 | see below |

**Image check, local.** Empty database. Container log shows the two `deploy_migrate` lines,
then gunicorn. PID 1 is gunicorn. `/healthz` 200, `/account/login/` **200**, `/admin/` without a
session 401 and one `WARNING django.request Unauthorized: /admin/` line in the log. With an
unreachable database the container exits 1 and gunicorn never starts.

**Tests:** 208 before, 238 after (236 unit, 2 e2e). No existing test edited, skipped or removed.
Every row of plan §5 has a passing test. Coverage tooling is not configured in this project;
by inspection every branch of the two new modules is exercised, including lock timeout,
release on failure and the non-PostgreSQL path.

**One test of mine was wrong and was corrected.** Row 11 first compared the
`django_migrations` row count with the plan length. The table also records squashed-away
migrations (35 rows against 27 plan entries). The test now compares against a database
migrated by a single run. The production code was not at fault.

**Gate 3, code quality:** diff read in full. Names and structure follow `backend/finance/`.
No dead code, no new dependency, no drive-by change. The logging module is named
`log_redaction.py`, the alternative the plan allowed, to avoid shadowing the standard library.

**Gate 4, security:**
- SQL is constant text with the lock key as a parameter.
- Secret scan of the diff and of every new file for the root `CLAUDE.md` §5.4 patterns: clean.
  New tests use placeholders only.
- No secret store was read by either model. No new secret name and no new environment variable.
- No new file type that can hold credentials.
- Access logging remains off. The webhook token inside a request path is redacted in the real
  process log (test 8).

**Findings**

```
[MINOR] backend/finance/management/commands/deploy_migrate.py:38 — a failure in the command is printed by the interpreter, not by the logging handler, so it is not passed through RedactingFormatter.
Failure scenario: a database driver error that quoted the connection password would reach the container log unredacted. Observed driver messages name host and port only; a run with a placeholder password showed 0 occurrences of it.
Required change: command failures are reported through the configured logger, or the traceback is redacted before it is written.
Decision: deferred to the Prompt 4 plan. Not reachable with today's driver messages.

[MINOR] backend/finance/settings.py:214 — every 4xx response writes a WARNING line, and every request with an unknown Host header writes an ERROR with traceback.
Failure scenario: internet scanners on the public hostname fill the container log and raise log cost.
Required change: DisallowedHost logged as one line without traceback; 4xx volume reviewed once real traffic exists.
Decision: deferred. Lines are redacted and carry the path only.

[MINOR] Dockerfile:86 — during the migration the shell is PID 1 and does not forward SIGTERM.
Failure scenario: a shutdown requested mid-migration waits for the platform's kill timeout.
Required change: none now; revisit with the startup probe.
Decision: deferred. Migrations take seconds today.
```

Counts: BLOCKER 0, MAJOR 0, MINOR 3 (all deferred by written decision above).

**Residual risk**
- Not yet verified on staging. Acceptance criterion 7 needs an approved push and is checked after it.
- A migration longer than about 120 seconds would be killed by the liveness probe. A startup
  probe is an `infra/` change for a later plan.
- Production start-up behaviour is reviewed again at Step 16. Staging holds no data.
- The Bicep template still rotates the database password on every infrastructure deployment
  (status note, defect 3). Unchanged by this work.

**Verdict: PASS** for merge to `main` and deployment to staging.

## QA Report — PLAN-003c: first admin on a deployed environment

**Date:** 2026-09-29 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-003c-first-staging-admin.md` (owner-approved 2026-09-29)

**Scope:** root project. Production (Opus): `backend/accounts/management/commands/bootstrap_admin.py`, `README.md`.
Tests: `backend/tests/test_bootstrap_admin.py` (Opus), `backend/tests/test_bootstrap_admin_adversarial.py` (Fable).
Docs (Fable): `infra/README.md` runbook section, this report.
Unchanged, verified: `pyproject.toml`, `uv.lock`, `.github/`, `Dockerfile`, `backend/finance/`, the middleware and the route registry.

**Commands run by Fable** (repository root, placeholder environment):

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .` | n/a | 0 | clean |
| `uv run ruff format --check .` | n/a | 0 | 85 files |
| `uv run mypy` (strict) | n/a | 0 | 58 source files |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 306 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 297 passed, 9 skipped (PostgreSQL-only) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 2 passed |
| both bootstrap test files, three consecutive runs | PostgreSQL 16 | 0 | 36 passed each time |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 272 before, 308 after. No existing test edited, removed or skipped. Every case of
plan §5 has a passing test. Coverage tooling is not configured; every branch of the command
is exercised.

**What the Fable tests add.** The implementation tests patch the prompt. Mine run the real
command on a real pseudo-terminal with `APP_ENV=staging` against PostgreSQL:
- the password typed at the prompt is never echoed to the terminal;
- a mismatch creates nothing;
- a second run exits 2 and changes nothing;
- **two runs released at the same moment create exactly one admin** (exit codes 0 and 2),
  which proves the row lock that sqlite cannot test;
- the password is absent from the process arguments and from the log line.

**Gate 3:** diff read in full. Follows `seed_local_admin.py`. No drive-by change.
**Gate 4:** no password argument, environment variable or non-interactive mode; email
validated and length-checked; existing users are never elevated; the check and the creation
share one transaction with a row lock; created user is not a superuser and has no device;
before enrolment it gets 403 on admin routes. Secret scan of the diff: two pattern hits, both
the placeholder constant in tests. No secret store read by either model.

**Findings:** BLOCKER 0, MAJOR 0, MINOR 0.

**Residual risk**
- `az containerapp exec` has not been exercised; it is the owner's step. If the platform
  session offers no terminal, the command refuses safely and creates nothing.
- Between creation and authenticator enrolment the password alone allows enrolment. The
  owner must enrol immediately. Accepted by the owner with the plan.
- The owner's email is written to the container log once (plan §3) and stored in the
  staging database.

**Verdict: PASS**

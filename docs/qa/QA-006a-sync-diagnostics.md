## QA Report — PLAN-006a: 4xx field names, retry lock, post-redirect-get, create ID in the log

**Date:** 2026-10-01 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-006a-sync-diagnostics.md`

**Scope:** `backend/payments/stax/transport.py`, `errors.py`, `operator_sync.py`; `backend/ledger/models.py`
(one guarded column), migration 0005 (column and trigger extension, reversible), `views/operators.py`,
the detail template. Tests (Opus): `test_stax_retries.py`, `test_stax_adapter.py`, `test_operator_sync.py`,
`test_operator_views.py`, `test_card_number_guard.py` (guard-module list). Tests (Fable): additions to
`test_stax_adversarial.py`. Unchanged, verified: `pyproject.toml`, `uv.lock`, `infra/`, `.github/`, `Dockerfile`, `backend/accounts/`.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 166 files |
| `uv run mypy` (strict) | n/a | 0 | 128 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 1,566 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 1,214 passed, 352 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| four affected files, two consecutive runs | PostgreSQL 16 | 0 | 228 passed each time |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 1,486 before, 1,569 after. Opus adjusted nine of its own earlier tests for the new
redirect-on-failure behaviour and the retry lock; none removed or skipped. Every row of plan §4 has a passing test.

**Fable's adversarial additions (14):** a 5 MB error body is handled in under a second and logged
as `non-json`; a card number, the configured API key, a routing-and-account pattern, empty, NUL,
unicode and deeply nested keys never reach the log; the logged field list matches `[A-Za-z0-9_.,-]+`;
a 2,000-level-deep body and scalar bodies do not crash. One of my cases first used the wrong
placeholder key; the code was right and the test was corrected.

**Findings:** BLOCKER 0, MAJOR 0, MINOR 0. Deviations accepted: `redacted` for keys with more
than 12 digits or holding the key; empty body and over-64 KB bodies report `non-json`; the
whole body is still downloaded, only the parse is limited; the lock applies to retries only;
`sync_failure_fields` appears in every operator audit snapshot.

**Gate 3 and 4:** extraction code read in full; only key names are read from a 4xx body, reduced
and cut; the service enforces the lock inside the row lock; every POST redirects; the session
holds invalid form data for one GET with any card number replaced by an empty string. Secret
scan clean. No secret store read.

**Residual risk:** whether Stax's 422 body names the offending field is unknown until the next
real 4xx; if it returns a message without field keys, the log shows `error_fields=message` and
the hypothesis is not confirmed by this change alone.

**Verdict: PASS**

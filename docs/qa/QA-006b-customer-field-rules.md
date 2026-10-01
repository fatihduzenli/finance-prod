## QA Report — PLAN-006b: Stax customer field rules validated before any request

**Date:** 2026-10-01 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-006b-customer-field-rules.md`

**Scope:** `backend/payments/stax/validation.py` (one phone rule, 255-character caps),
`adapter.py`, `mapping.py` (no empty optional fields sent), `operator_sync.py`, `backend/ledger/forms.py`,
`views/operators.py`, the detail template, migration 0006 (normalise stored phones, flag bad ones,
reversible no-op). Tests (Opus): `test_stax_validation.py`, `test_stax_adapter.py`, `test_operator_sync.py`,
`test_operator_views.py`. Tests (Fable): additions to `test_operators_adversarial.py`.
Unchanged, verified: `pyproject.toml`, `uv.lock`, `infra/`, `.github/`, `Dockerfile`, `backend/accounts/`.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 169 files |
| `uv run mypy` (strict) | n/a | 0 | 129 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 1,631 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 1,279 passed, 352 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| four affected files, two consecutive runs | PostgreSQL 16 | 0 | 279 passed each time |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 1,569 before, 1,634 after. No existing test edited, removed or skipped. Every row of
plan §3 has a passing test. Fable's additions: eleven hostile phones (full-width and Arabic-Indic
digits, plus signs inside, a Luhn-valid 16-digit run, a 9-digit run, 16 digits, an extension,
a `tel:` prefix, a zero-width character) are refused with the phone message, never the card
message, and make no provider call; four accepted formats reach the provider as digits only.

**Findings:** BLOCKER 0, MAJOR 0, MINOR 0. Deviations accepted: the form and the migration import
the rule through `payments.operator_sync`'s re-export (Rule 8 isolation); a bad stored phone
refuses a sync with the local code `PhoneInvalid` and writes nothing; empty name and email
fields are omitted from the payload, not only the last name; `cc_emails` is still sent as an
empty list, acceptable (an empty array is not an empty string and the spec types it as an array).

**Residual risk:** Stax's own response to the normalised payload is unverified until the next
save on staging; the owner's successful retry with a completed phone is the current evidence.

**Verdict: PASS**

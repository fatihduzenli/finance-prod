## QA Report — PLAN-007c: large-debit warning with admin confirmation; confirmed limit as hard block

**Date:** 2026-10-01 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-007c-large-debit-confirmation.md`

**Scope:** `backend/finance/env_guard.py`, `settings.py` (`PROVIDER_MAX_TXN_AMOUNT` from the
environment variable `STAX_MAX_TXN_AMOUNT`), `backend/ledger/services/leases.py`, `forms.py`,
`views/leases.py`, `services/rent_run.py`, templates `enrol.html` and `run/today.html`, `.env.example`,
`README.md`, `infra/main.bicep`, `infra/modules/containerAppWeb.bicep`, `infra/parameters/staging.bicepparam`
(comment only). Tests (Opus): `test_lease_services.py`, `test_lease_views.py`, `test_run_views.py`,
`test_env_guard.py`, `test_rent_run.py`. Tests (Fable): additions to `test_rent_run_adversarial.py`.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 200 files |
| `uv run mypy` (strict) | n/a | 0 | 154 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 2,043 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 1,681 passed, 362 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| `az bicep build --file infra/main.bicep` | n/a | 0 | compiles |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 1,983 before, 2,048 after. No test removed or skipped; one existing assertion of
Opus's updated for the replaced page text, reported.

**Findings**

```
[MINOR] backend/ledger/forms.py — Django's checkbox widget counted the posted string "0" as a confirmation.
Failure scenario: confirm_large_debit=0 posted by an admin's client enabled a large debit as if confirmed.
Status: RESOLVED. The confirmation is accepted only for the exact values "on" or "true"; thirteen other values are tested as non-confirmations. Found by a Fable adversarial test; my own test had "1" as confirming and was corrected to the rule.
```

Counts: BLOCKER 0, MAJOR 0, MINOR 1 resolved. Deviations accepted: the Django setting is
named `PROVIDER_MAX_TXN_AMOUNT` (the ledger may not contain the provider's name); amounts with
fewer than two decimals are accepted by the guard; comparisons are strict, so an amount equal
to a threshold passes; refusals for a missing confirmation are not audited, the confirmation is.

**Owner's walk-through evidence recorded:** store and lease created; rent history correct; run
page listed the lease and a missing premium; charges idempotent; match read zero schedules; the
old hard block fired at $2,100; the sandbox dashboard cannot tokenise a bank account ("no
tokenization gateway found"), so enrolment stays blocked on Stax.

**Verdict: PASS**

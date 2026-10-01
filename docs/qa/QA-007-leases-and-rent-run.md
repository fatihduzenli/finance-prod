## QA Report — PLAN-007: lease setup, autopay enrolment, monthly rent run

**Date:** 2026-10-01 · **QA owner:** Fable · **Implementer:** Opus (two batches, plus a review pass) · **Plan:** `docs/qa/PLAN-007-leases-and-rent-run.md`

**Scope:** `backend/ledger/services/` (`leases.py`, `rent_run.py`, `insurance.py`, `numbering.py`),
`views/leases.py`, `views/run.py`, `views/charges.py`, forms, templates, urls, migration 0007
(`collection_schedules.needs_review`), `management/commands/generate_insurance_charges.py`;
`backend/payments/` (`provider.py` payment methods, `stax/adapter.py`, `stax/mapping.py`, `fake.py`,
`invoice_sync.py`, `health.py`, `stax/breaker.py` read-only `is_open()`); `backend/accounts/registry.py`
(rules); `backend/finance/` (`env_guard.py`, `settings.py`, base template nav); `.env.example`; `README.md`.
Tests (Opus): `test_lease_services.py`, `test_lease_views.py`, `test_stax_payment_methods.py`,
`test_rent_run.py`, `test_insurance_charges.py`, `test_run_views.py`, `test_invoice_numbering.py`,
plus small updates to `test_env_guard.py`, `test_provider_interface.py`, `test_stax_adapter.py`,
`test_operator_views.py`. Tests (Fable): `test_rent_run_adversarial.py`.
Unchanged, verified: `pyproject.toml`, `uv.lock`, `infra/`, `.github/`, `Dockerfile`, auth middleware and models.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 191 files |
| `uv run mypy` (strict) | n/a | 0 | 149 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 1,905 passed, 0 skipped (see the flake note) |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 1,544 passed, 361 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| five Prompt 7 test files, two consecutive runs | PostgreSQL 16 | 0 | 204 passed each time |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Flake note.** The first full PostgreSQL run after my adversarial file was added showed
one failure out of 1,905. I did not capture its name, which is my mistake. Eleven further
full runs on PostgreSQL, all 1,905 tests, were green. The project rule treats a flaky test as
a failure, so this is recorded as an open MINOR finding below rather than dismissed: the CI
log of every push keeps the name if it recurs, and the next QA report must name it or close it.

**Tests:** 1,634 before, 1,908 after. No test removed or skipped. Opus adjusted four of its
earlier tests for legitimate reasons (route prefix narrowing, the future-rent decision, a
pinned date) and reported each.

**Defects found and fixed before the gate:** Opus's review pass found that the lease row lock
used `select_for_update` across a nullable join, which PostgreSQL refuses; enable, pause and
resume would have crashed on every call on staging. Fixed with `of=("self",)` and covered by a
PostgreSQL concurrency test. Fable's tests then passed on their first corrected run; two of my
cases were wrong (field name, resume semantics) and were corrected.

**Fable's adversarial tests (22):** two concurrent enable clicks make one schedule per debit;
a method of another customer, a card and an unknown ID are refused with no create; the enrol
form cannot tamper with the schedule total; a rent change never edits a schedule and resume is
refused while flagged; resume never carries an amount; three concurrent runs create each
charge once; a run on a day with nothing due writes nothing; month-boundary invoice dates are
never guessed; matching three times links once and keeps the number; `provider_status` cannot
be written from the ledger (trigger); ten bad manual payments save nothing (over-payment, zero,
negative, half cent, blank note, card number in the note, card, ach and cash methods); three
concurrent partial payments never exceed the balance; viewer and billing cannot change leases
or enrol.

**Findings:** BLOCKER 0, MAJOR 0, MINOR 1 open (the row-lock defect was fixed before the gate).

```
[MINOR] backend/tests — one unidentified test failed once in twelve full PostgreSQL runs.
Failure scenario: unknown; not reproduced in eleven consecutive runs of the same suite on the same database.
Required change: name it on recurrence (CI keeps the log) and fix or isolate it in the next plan.
Decision: open; recorded here and in docs/STATUS.md.
```
Deviations accepted, the material ones: debit days 29 to 31 are refused for autopay until
TODO-VERIFY-036 is known; a retry after an unanswered create adopts exactly one matching
unlinked provider schedule and refuses on more; matching is refused while the breaker is open
or the budget exhausted; invoices from paused schedules are not matched while paused;
`internal_status` of a matched invoice stays `draft`; numbering skips numbers already taken.

**Prompt 7 stop conditions:** answered in plan §2, not stopped on. The payload differs from the
indicative one (`url` required, `memo`/`reference` under `meta`, `item`/`price` line items,
`invoice_date_at`/`due_at`); the per-transaction limit is still unrecorded (placeholder
threshold, labelled on screen); the human-readable number lives in `meta.reference`.

**Residual risk**
- Nothing has run against Stax. Sandbox ACH is reported disabled; the first real enrolment is
  expected to fail at the schedule create with field names until Stax enables ACH (plan D12).
- TODO-VERIFY-035 to 038 (schedule `meta`, days 29 to 31, bank method last-four field, invoice
  list keys) are assumptions until the first real schedule and invoice.
- Insurance autopay schedules are a later plan (D9); insurance charges with no amount are not
  updated when the premium arrives later; two leases overlapping on one store at an anniversary
  would both be charged.
- Pagination of the invoice list (TODO-VERIFY-021) is still first page only.
- Deferred MINOR findings across earlier reports: nine, unchanged.

**Verdict: PASS**, with the unidentified single failure carried as an open MINOR finding.

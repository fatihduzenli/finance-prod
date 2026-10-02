## QA Report — PLAN-009: payments ledger, external payments marked in Stax, refunds and voids

**Date:** 2026-10-02 · **QA owner:** Fable · **Implementer:** Opus (two batches) · **Plan:** `docs/qa/PLAN-009-payments-ledger.md`

**Scope:** `backend/ledger/services/rent_run.py` (marking, retry, carried balance), `services/payments_ledger.py`,
`services/refunds.py`, `views/payments.py`, `views/charges.py`, `views/leases.py`, forms, templates
(`payments/list|detail|refund|void|summary`, charges, leases, run), `urls.py`, migration 0010
(`rent_charges.review_message`, guard trigger on `rent_charges`), `backend/payments/offline_marking.py`,
`invoice_sync.py`, `stax/mapping.py` (no `apply_balance`), `stax/validation.py`, `fake.py`,
`webhooks/handlers/transaction.py` (refund and void helpers shared with the admin actions),
`accounts/registry.py`, base template, `README.md`. Tests (Opus): `test_manual_payment_marking.py`,
`test_payments_ledger.py`, `test_refunds_voids.py`, small updates to `test_card_number_guard.py`,
`test_run_views.py`, `test_stax_adapter.py`. Tests (Fable): `test_payments_adversarial.py`.
Unchanged, verified: `pyproject.toml`, `uv.lock`, `infra/`, `.github/`, `Dockerfile`, auth middleware.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 242 files |
| `uv run mypy` (strict) | n/a | 0 | 191 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 2,402 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 2,025 passed, 377 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 2,252 before, 2,407 after. No test removed or skipped.

**Confirmed endpoint paths** (specification read 2026-10-02): `POST /invoice/{id}/pay/{method}` with
`method` in `cash`, `check`, `giftcard`; query `payment_note` (255), `check_number` (25),
`email_receipt`; `apply_balance` exists but its unit is unstated and it is **not used**.
`POST /transaction/{id}/refund` with body `total`; partial refunds allowed up to the remaining
amount. `POST /transaction/{id}/void`, whole amount only. TODO-VERIFY-016 is closed; 028 stays open.

### Scenario matrix

| # | Scenario | Test | Sandbox |
|---|---|---|---|
| 1 | Full manual payment on an invoiced charge → marked `cash` in Stax between two re-fetches, memo "Paid by wire on MM/DD/YYYY. note", invoice PAID via the sync writer | pass | provable now |
| 2 | Partial manual payment → local only, charge partial, flag "Provider invoice not updated: partial amount" | pass | provable now |
| 3 | Two partials completing the charge → paid, no Stax mark, flag stays until TODO-028 | pass | provable now |
| 4 | Full ACH settlement, partial refund → refund row linked, charge partial, invoice re-fetched | pass | blocked on ACH |
| 5 | Refund of a partial ACH payment | pass | blocked on ACH |
| 6 | Void of a pending ACH payment → original voided, charge reopened | pass | blocked on ACH |
| 7 | Manual payment for an invoice Stax already shows paid → kept locally, mismatch audited, no second mark | pass | provable now |
| 8 | Pay half, refund the half, pay in full → ends paid, balance 0, three rows | pass | partly blocked |
| 9 | Refund above remaining, three decimals, negative, zero, text → refused before any call | pass | unit |
| 10 | Billing and viewer on refund and void → 403 on GET and POST, no button, no call | pass | unit |

**Fable's adversarial tests (20):** a card number in the note reaches neither the ledger nor Stax;
six odd references are reduced to the 25-character safe form and never refused locally; a
replayed manual payment marks Stax once; a failed re-read after a good mark does not mark twice
on retry; the export round-trips notes with commas, quotes and newlines, neutralises a leading
`=`, never unmasks a provider ID, and completes 5,000 rows well inside the bound; every refund
and void route is closed to viewer and billing on GET and POST; six bad refund amounts make no
call; a refund equal to the remaining amount succeeds and one more cent is refused.

**Findings:** BLOCKER 0, MAJOR 0, MINOR 0. Deviations accepted, the material ones: a void marks
the original payment `voided` instead of adding a row (a void is not a new transaction at Stax
and the webhook handler models it the same way; **the owner is asked to confirm**, since
amendment 4 says new rows for both); refundable amount subtracts pending refunds too; a refund
row is `pending` until the re-fetch or webhook shows it settled, so the charge reopens then; a
full payment whose amount differs from the invoice total is not sent to Stax and is flagged; the
export prefixes cells starting with `= + - @`, tab or CR with an apostrophe.

**Residual risk**
- Partial payments are never marked in Stax until TODO-VERIFY-028 (`apply_balance` unit) is
  settled in the sandbox; such invoices stay unpaid on the Stax side and flagged on ours.
- A manual payment recorded before its invoice is matched is not sent to Stax later.
- Refund and void against real Stax are blocked on sandbox ACH (TODO-VERIFY-046, 048).
- TODO-VERIFY-055: the paid status string is `PAID`.
- Deferred MINOR findings from earlier reports: ten, unchanged.

**Verdict: PASS.** Staging acceptance is the owner's four clicks.

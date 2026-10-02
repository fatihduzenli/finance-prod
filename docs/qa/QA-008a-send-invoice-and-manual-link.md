## QA Report — PLAN-008a: send_invoice object ID, manual invoice linking, auto-link

**Date:** 2026-10-02 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-008a-send-invoice-and-manual-link.md`

**Scope:** `backend/payments/webhooks/receiver.py` (nested object-ID paths, source recorded),
`events.py`, `processor.py` (event time in a context variable), `handlers/invoice.py` (derived
`sent_at`/`viewed_at`/`paid_at`, auto-link), `invoice_sync.py`, `models.py` and migration
`payments.0004` (`object_id_source`, `outcome` widened to 64), `backend/ledger/services/rent_run.py`
(`unlinked_invoices`, `auto_link_invoice`, `link_invoice`), `models.py` and migration `ledger.0011`
(one invoice per rent charge, partial unique constraint), `forms.py`, `views/charges.py`, `urls.py`,
`accounts/registry.py`, templates (link page, charge page, run page, webhook detail).
Tests (Opus): `test_webhooks.py`, `test_webhook_handlers.py`, `test_run_views.py`, constraint cases.
Tests (Fable): additions to `test_rent_run_adversarial.py`.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .` | n/a | 0 | clean, 248 files |
| `uv run mypy` (strict) | n/a | 0 | 193 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 2,520 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 2,141 passed, 379 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Tests:** 2,445 before, 2,525 after. One existing assertion updated by Opus for the new outcome
text; no test removed or skipped. Every row of plan §3 has passing tests.

**Facts from staging recorded** (plan §1): event name in the header; `create_invoice` body is the
bare object with `total_paid`, `balance_due`, `schedule_id`, `payment_method_id` and no
timestamp keys; `send_invoice` body is an email envelope with the invoice under
`mergeVars.invoice`. TODO-VERIFY-015 and 042 (invoices) closed; 044 rewritten; 057 added.

**Findings:** BLOCKER 0, MAJOR 0, MINOR 0. Deviations accepted: the event time reaches the
handler through a context variable rather than a third argument (handlers still never see the
body); two migrations because the tables live in two apps; `outcome` widened to 64; `paid_at` is
derived only on invoice-handler re-fetches; auto-link compares the provider's total and date.

**Fable's adversarial additions (3):** an invoice of another operator posted by ID is refused;
a note with a card number and four tampered confirmation values link nothing on a differing
total; two charges racing for one invoice leave it on exactly one (PostgreSQL, unique constraint
and row locks).

**Residual risk:** `sent_at` is the time our receiver got the send event, not Stax's send time
(the object carries none). Staging acceptance is the owner's: replay the stored `send_invoice`
event, link the $1,500 invoice, record the wire payment.

**Verdict: PASS**

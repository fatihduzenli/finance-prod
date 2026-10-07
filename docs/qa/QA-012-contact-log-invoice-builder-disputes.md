## QA Report — PLAN-012: contact log maturity, the invoice builder, the dispute list

**Date:** 2026-10-07 · **QA owner:** Fable · **Implementer:** Opus
**Plan:** `docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md` · **Decision:** ADR-0011

**Scope.** Backend only; no infrastructure, no workflow, no new environment variable, no new
dependency. Three batches:

| Batch | What | Main files |
|---|---|---|
| D1 (committed `0f2b6b4`) | `docs/BACKLOG.md`, `docs/PROVIDER.md`, the portability test | `tests/test_provider_portability_adversarial.py` |
| A | Contact log: 15-minute edit window, appended corrections, admin soft delete with a reason, store/lease on entries and the store page's section, automatic promise close by a settled payment, previous-promise outcome (Kept / Rescheduled / Broken), status filter | `ledger/migrations/0028_contact_log_maturity.py`, `ledger/services/contacts.py`, `ledger/views/contact_log.py`, `ledger/templates/ledger/contacts/*`, `tests/test_contact_log.py` |
| B | Invoice builder replacing "Create one-off charge" (410 on the old addresses): drafts with lines, preview and print, Send → one Stax invoice with every line and one one-off charge per line, Charge now (admin), cancel; sibling allocation generalised to a builder invoice's lines; deposits held, the ledger total, the P&L footer and the statement read the lines; the returned-then-rescheduled spill reads the lines | `ledger/migrations/0029_invoice_builder.py`, `ledger/services/invoice_builder.py`, `ledger/views/invoices.py`, `ledger/templates/ledger/invoices/*`, `ledger/static/ledger/invoice_lines.js`, `ledger/services/{one_off_charges,payments_ledger,pnl,reports,receivables}.py`, `payments/one_off_invoices.py`, `tests/test_invoice_builder.py` |
| C | Dispute list with evidence upload: `PaymentProvider.list_disputes` and `upload_dispute_evidence` (Stax adapter and fake), a multipart body in the one transport, `disputes` and `dispute_evidence` tables, the operator as a document owner for dispute evidence only (TIFF for that kind only), jobs `disputes_sync` and `dispute_evidence_upload`, the `/disputes/` page and CSV, the daily and webhook-triggered sync | `ledger/migrations/0030_disputes.py`, `payments/migrations/0015_dispute_jobs.py`, `ledger/services/disputes.py`, `ledger/views/disputes.py`, `ledger/templates/ledger/disputes/page.html`, `payments/{provider,errors,fake}.py`, `payments/stax/{adapter,mapping,transport,validation}.py`, `ledger/services/documents.py`, `tests/test_disputes.py` |
| QA | Adversarial cases (plan §6, Fable) | `tests/test_plan012_adversarial.py` (18 functions, 35 cases) |

**Commands run (final code, after the last rework).** Env: `APP_ENV=local STAX_ENV=sandbox
STAX_API_BASE_URL=https://apiprod.fattlabs.com APP_BASE_URL=http://localhost:8000
DJANGO_SECRET_KEY=placeholder-not-secret STAX_API_KEY=FAKE-CI-KEY-XYZ123`, plus a scratch
`DATABASE_URL` for `manage.py`; PostgreSQL 16 in a throwaway container on 127.0.0.1:55432.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .` | n/a | 0 | clean |
| `uv run ruff format --check .` | n/a | 0 | 419 files already formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 345 source files |
| `uv lock --check` | n/a | 0 | unchanged |
| `manage.py makemigrations --check --dry-run` | sqlite | 0 | No changes detected |
| `manage.py makemigrations --check --dry-run` | PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 4,483 passed, 560 skipped (4,232 before) |
| same | PostgreSQL 16 | 0 | 5,040 passed, 3 skipped (4,753 before) |
| `uv run pytest -m e2e -q` (phone and laptop, Chromium) | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | OK |
| Pattern secret scan of the diff and every new file (AccountKey, SAS, private keys, `sk-`, `ghp_`, `AKIA`, `client_secret`, `password=`, bearer literals, connection strings with credentials) | n/a | 0 | no hit |
| `TODO-VERIFY` comments in code all have a register row | n/a | 0 | yes (062, 063 added) |
| Bicep build | n/a | — | not applicable: `infra/` untouched |

Baseline before PLAN-012 (QA-011b-a): PostgreSQL 4,753 passed; sqlite 4,232 passed.

**Tests (Gate 2).** Opus: `test_contact_log.py` 33 functions, `test_invoice_builder.py` 43,
`test_disputes.py` 38, plus constraint cases in `test_ledger_models.py` and list updates in
`test_card_number_guard`, `test_migrations_roundtrip`, `test_provider_interface`,
`test_csv_export`, `test_stax_adapter` (the verified-endpoints test now also reads
`docs/BACKLOG.md` §1 row 4; the two calls are also recorded in
`docs/inputs/STAX_ENDPOINTS_2026-09-30.md`). Every §6 case named for the three batches has a
test: the 14:59 / 15:01 window with a pinned clock; a payment exactly equal on the promised
date; a lease ended between promise and payment; zero, one and two active leases; a draft with
one and 20 lines; quantity 0.5 and 3; totals to the cent; a due date in the past (refused) and
absent (today + 15); `respond_by` today, in 6 days, in 8 days, past; evidence of 10 MB and
10 MB + 1 (both refused: Stax says "< 10 MB", so 10,000,000 bytes is refused and 9,999,999
accepted); a .tiff accepted, a .docx refused; the provider 400 after `respond_by` shown; edit
by another author 403; delete by billing 403; a correction on a deleted entry refused; send
twice → one invoice; a line with a card number refused; cancel after a payment refused; the
provider down → the page keeps the last fetch and its time.

Fable's adversarial file (`test_plan012_adversarial.py`): a line worth nothing, less than
nothing or a fraction of a cent is refused (13 shapes, including NaN and infinities); the
exponent form of a cent amount and a half-cent total are legal money; a tampered stored line
total is refused by the table's CHECK and the sent lines sum to the cent through the fake's
single create; a draft of operator A cannot be made or sent (or charged) against operator B's
lease, even with the lease swapped behind the service; an ended lease takes arrears by e-mail
and the builder has no debit-on-due-date collection at all; a viewer cannot make, edit, send,
charge or cancel a draft nor edit, correct or delete a contact entry, billing cannot delete,
and the author cannot reopen the window with a forged timestamp; a deleted entry's text and
reason reach no viewer page or CSV and the deleted promise is no broken promise; a PDF with
the routing-and-account pair in page text, a form field, an annotation, the title or split
across pages never reaches the provider through the route (no document, no blob, no job, no
number in the audit log); an evidence name with a card number and an upload past
`respond_by` store nothing; the page and CSV show no unmasked transaction or dispute id even
when the summary carries them, the page escapes the provider's words and the CSV neutralises
a formula; hostile provider values (300-character reason, control characters, no amount, an
unknown transaction, a status that is a card number) neither crash the sync nor enter the
rows; the sync changes no payment or charge state; viewers and anonymous visitors get nothing
from the dispute routes; a multipart name with a line break sends nothing and bytes that look
like a boundary stay one part; a refusal sentence carrying a card number is dropped; the
provider's late-upload refusal shows as a sentence, never a traceback, and the file stays
ours; both providers implement the dispute methods with the interface's signature.

No test was skipped, weakened or deleted; `test_collections.tick()` deletes the newly queued
dispute-sync job so the ladder tests stay about mail (reviewed: it does not hide a failure).
Coverage is not measured in this repository (no coverage tool); every new branch named above
has a test.

**Findings (Gates 3 and 4).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 1 | MINOR | `reports.py` deposits held | A deposit on a later builder line was not counted as held (the query read the payment's charge) | Resolved in Batch C: one shared split helper in `payments_ledger`, used by the ledger total, the P&L footer and the statement; tests for a deposit as the second line paid in full, in part, and on the first line only |
| 2 | MINOR | `receivables._spill_to_companions` | Knew only `companion_of`; a returned, rescheduled payment on a multi-line builder invoice would double count | Resolved in Batch C: `_billed_siblings` (companion fees, then later lines in order); the two-line paid, returned, rescheduled case owes once |
| 3 | MINOR | `disputes._summary` | The stored summary kept whatever keys the provider record carried (the fake's `transaction_id`, `id`, `merchant_id` reached the row unmasked; the Stax mapping trims them, the ledger did not) | Resolved: the ledger trims to the documented fields plus the masked transaction, whatever the adapter sends |
| 4 | MAJOR | `disputes.py` comment | The rework's comment named the provider and broke the isolation fence (`test_stax_isolation`) | Resolved: reworded; fence green |

Design choices accepted as built: `changed_at` rather than `provider_updated_at` (the schema
fence); the evidence checkbox required for images only (PDFs are text-checked); the operator
as a third document owner holding dispute evidence only; the same file refused twice for one
operator; the webhook handler importing the dispute service inside the function (no cycle);
Charge now on a draft queuing the single pay attempt after the create, the admin's
confirmation carried in the job's params (ADR-0009's one request, no retry).

Gate 4 notes: every route has a registry rule and the views check the role again; input is
typed at the boundary (forms are shape-only, the services decide; names, memos, descriptions
and reasons go through the payment-data guard; file type is sniffed, never trusted from the
name; size checked before and after reading); provider identifiers are masked on pages,
CSVs and audit rows; the provider's refusal sentence is read only when it matches the named
pattern, is short, holds no payment data and not the key, and it is never logged; the upload
is not retried after a 5xx or timeout; the sync is one job at a time, paged, capped at 50
pages; the daily sync is queued once a day; row locks are taken for the draft, its operator
and the evidence row; the evidence upload holds its row lock across the provider call (a
retry by an admin waits, then sees `sent` and is refused: acceptable for a single worker;
noted); no new dependency; no new environment variable; `.env.example` unchanged; no file
type capable of holding credentials added.

**Confidentiality gate (§5.4).** Secret scan clean (table above). No secret-store read by
either model during the task. No new credential-capable file type. No new secret name.

**Residual risk.**
- TODO-VERIFY-062 (the upload response's file `id`) and 063 (the wording of the late-upload
  400) are assumptions until the first evidence upload on a real dispute; a wrong assumption
  leaves the evidence row `sent` without a file reference, or shows the refusal by class name
  instead of the sentence. Neither loses the file: it stays our document.
- Whether `endDate` on `GET /underwriting/disputes` is inclusive, and the pagination shape
  beyond "stop when a page adds nothing", are unverified on a real account; the window is
  90 days with a 50-page cap.
- The dispute list on the sandbox is expected empty; the §8 click "the dispute list renders"
  proves the page, not a dispute.
- The browser e2e suite was not extended for the disputes page (Django-client tests only);
  the builder's pages are covered by Django-client tests and the existing e2e shells.
- Deposits on a second line are now counted as held; `receivables.paid_in` and
  `reports.payments_by_method` still exclude deposits by the payment's own charge only, so a
  deposit billed as a later line appears in "paid this month" and in payments by method
  (recorded; a later plan if the owner bills deposits that way).
- S1 (line-item `kind`) and S2 (operator consent columns) are the owner's pending decisions;
  nothing in this change depends on them.

**Verdict: PASS** (pending the owner's staging clicks of plan §8 and the owner's S1/S2
answers for `docs/DECISIONS.md`).

## QA Report — PLAN-012b-b: rent run catch-up, rent billed by invoice, lease-end wizard fixes, P&L at lease end, splitting rent

**Date:** 2026-10-15 · **QA owner:** Fable · **Implementer:** Opus · **Plan:**
`docs/qa/PLAN-012b-b-catch-up-rent-lines-wizard-fixes.md` · **Decision:** ADR-0012 §7

### Batch A — catch-up (D1–D3), the draft's lifecycle (D9), the final rent charge (D10), the unpaid deposit (D11)

**Scope.** `backend/ledger` (`services/rent_run.py`: `create_month`, `catch_up_lease`,
`debit_date`, `ChargeRunReport.caught_up`; `leases.py` create/update call the catch-up;
`lease_end.py`: `DEPOSIT_UNPAID`, `in_progress`, `store_in_progress`, `adopt_for_transfer`,
`open_statement`, `sweep_drafts`; `statements.py`: `OpenLine.url`, `DepositLine.charged/text`;
`transfer.py`: adoption, the "not charged yet" branch dropped; views `lease_ends`, `transfers`,
`stores`, `run`; templates; `management/commands/collections_tick.py`: charge creation and the
draft sweep; `models.py` `LeaseEnd.statement_opened_at`; migration 0040), tests (two new files;
fixtures of `make_lease` and the rent-run lease builder now avoid the first charge on save by
saving before the start, with explicit starts where a test needs an earlier month; no test
skipped or deleted).

**Decisions in the gate.** (1) The daily job never created rent charges before this batch (only
the button did); the owner's text says "daily job and the button", so `collections_tick` now
calls `create_charges` first. **For the owner:** the first tick after the deploy creates every
missing month (bounded to 24) for every active lease with a start date; on staging that is test
data. (2) A deposit from the setup checklist cannot be cancelled, so the blocker reads
"…not collected: collect it, or waive or cancel the charge." (3) The due date of a charge made
by the wizard follows the last-month rule (the debit day, or the end date when earlier, never
before a start in the same month).

**Commands run (final Batch A code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 474 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 383 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 1 | 5,075 passed, 620 skipped, 1 failed: Fable's `test_setup_fees_adversarial` deleted leases that now carry their first charge (test fixed: the charges go first); the file re-run green on both engines; 6:52 wall |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 22 passed; 5:13 wall |
| the "Unit tests" selection | PostgreSQL 16 | 1 | 5,670 passed, 3 skipped; the 13 failures were the same setup-fees test and the 12 cases of Fable's Batch B adversarial file, written ahead of Batch B and parked until it lands; 9:31 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `1e1ed40` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_rent_catch_up.py` (16), `test_lease_end_drafts.py` (14), the
tick's charge-creation cases, seeded round trip of 0040, updated expectations in
`test_proration`, `test_run_views`, `test_rent_run`, `test_transfer`. Fable
(`test_plan012b_b_a_adversarial.py`, 5 + 1 PostgreSQL-only): the catch-up never creates a charge
for another lease's month or past the end and is idempotent; two runs at the same moment create
each charge once (threads, PostgreSQL); an adopted draft is bound to its own store, blocks no
transfer and the plain wizard defers to the transfer; a viewer sees the banner without the
Cancel link and gets 403 on it; the final charge is one of 31 days whatever the deductions post
and never doubles; the deposit blocker is not cleared by a part payment and clears when paid.

**Findings (A).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 1 | decision | plan D3 | No daily tick created charges; the plan assumed one | Resolved: the tick creates charges first (owner's text), tested |
| 2 | MINOR | `lease_end.DEPOSIT_UNPAID` text | "cancel the charge" is not possible for a setup-checklist deposit | Resolved: "collect it, or waive or cancel the charge" |

Design choices accepted: the fixture changes (leases saved the day before their start);
`debit_date` (a start-month debit before the start is due on the start date); the statement's
due-date rule; the 24-month bound stated on the run page.

**Verdict (A):** PASS (the one failure was Fable's own test fixture; no production finding beyond the two above).

### Batch B — rent billed by invoice (D4–D8), the P&L at lease end (D12)

**Scope.** `backend/ledger` (`invoice_builder.py`: the Rent line, `_clean_rent_line`,
`_check_rent_lines`, `rent_charge_refusal`, `_rent_charge`, `sent_rent_charges`;
`one_off_charges.py`: the sibling allocation with Rent lines (`rent_share`, `invoice_line_count`,
`invoice_covered`, `cancel_refusal`); `rent_run.py`: `own_paid`, `charge_invoices` through line
items; `payments_ledger.invoice_line_parts` with Rent parts; `pnl.py`: ADR-0012 §7 and the Rent
part as rent collected; `grid.py`: the invoice number; `transfer.balance_blockers` after the
offset; `lease_end.py`, `installments.py`, `views/invoices.py`, `views/charges.py`, forms,
templates, `invoice_lines.js`; `models.py`: `InvoiceDraftLine.rent_lease/rent_month`,
`InvoiceLineItem.rent_charge`, `RentCharge.billed_by_invoice`, two CHECKs; migration 0041),
`backend/payments` (`one_off_invoices.py`: Rent lines in the invoice body, `create_rent_invoice`,
`send_rent`; `jobs`: a rent charge as the entity of `invoice_create`/`invoice_send`; the
transaction webhook's allocation audit), tests. No new dependency or environment variable; no
provider call the adapter did not already make.

**D12 reproduction (Opus, `tests/test_pnl.py` Store Three cases).** The owner's "Fees billed
1300.00 / Fees collected 1500.00 incl. 500.00 withheld" came from the first rent billed as a
typed $1,000 builder item "Rent" (category `other`: $1,000 billed and collected as a fee), the
$300 lease-end balance billed, and the $500 withheld deposit collected but never billed. Rows
before (October, Northwind, no offset): rent billed 32.26, rent collected 32.26, fees billed
1300.00, fees collected 1500.00 (incl. 500.00 withheld). After ADR-0012 §7 with the typed line
gone (the Rent line replaces it): rent billed 32.26, rent collected 32.26, fees billed 800.00,
fees collected 500.00 (incl. 500.00 withheld deposit), deposits held 0.00; with the wizard's
pre-filled $32.26 offset: fees collected 467.74 (incl. 467.74 withheld), rent collected 32.26
from the offset. The incoming lease's row carries its prorated first month (1370.97) as rent
billed. **For the owner:** the typed "Rent" line on staging's Test Store Three stays a fee in
history; new first rents go through the Rent line.

**Commands run (final Batch B code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 476 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 385 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 5,113 passed, 621 skipped; 7:02 wall |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 23 passed; 5:31 wall |
| the "Unit tests" selection | PostgreSQL 16 | 0 | 5,708 passed, 3 skipped; 9:52 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `13b60fd` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_invoice_builder.py` (+18), `test_pnl.py` (+3 Store Three),
`test_ledger_models.py` (the two CHECKs), seeded round trip of 0041. Fable
(`test_plan012b_b_b_adversarial.py`, 12 cases): a rent line cannot bill another operator's lease
or a lease that does not exist; the amount stays within a cent and `AMOUNT_MAX` (zero, a
thousandth, negative, a million, NaN refused); a forged month (the 13th, the 31st, 1999) is
refused; two rent lines for one month are refused on create, on update in another position and
across drafts; the invoice's rent charge is never billed or paid twice by the run (the run on
the start date and later creates nothing, the invoice's payment pays the charge, a manual payment
is then refused, the P&L bills and collects the month once as rent); a move-in invoice counts
each kind once (rent as rent, the setup fee as fees, the deposit held, the catch-up adds nothing).

**Findings (B).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 3 | MAJOR | `transfer.balance_blockers` | The transfer's open-balance blockers were judged before the unpaid-balance offset, so a balance the deposit covers still blocked the transfer (found by Opus's D12 reproduction) | Resolved: the offset is applied to the open lines, oldest first, and only the remainder blocks |

Design choices accepted: the rent share of a mixed invoice is derived as the one-off lines'
shares are (no second payment row, so no double count); a lease with a live rent autopay
schedule and a twice-monthly lease take no Rent line; rent alone allows one Rent line; Charge
now and the builder's cancel of a rent-alone invoice are refused (the release of Batch C covers
rent invoices); `Invoice.rent_charge` only on a rent-alone invoice (the one-source CHECK), mixed
invoices link through the line item; the end month of an ended lease defaults to the full rent,
editable.

**Verdict (B):** PASS (one MAJOR found by the reproduction and resolved; residual risk: the browser behaviour of the Rent kind's JavaScript is checked by the owner's click).

### Batch C — splitting rent (D13)

**Scope.** `backend/ledger` (`services/leases.py`: `move_autopay`, `debit_instant`,
`autopay_move`, the moved-schedule audits; `installments.py`: rent parents in `release_kind`,
`release_notice`, `release_pending`, `_request_release`, `after_release`, `_rent_refusal`;
`one_off_charges.parent_paid_on_plan` for rent parents; `rent_run.recompute_charge` applying the
plan status; `attention.py` the `split_charge_paid` item for rent parents; views `charges`,
`leases`; templates; `urls.py`, `accounts/registry.py` (`ledger:charges:retry_release`)),
`backend/payments` (`one_off_invoices.py`: `release_invoice`/`release_schedule` by entity type,
`release_rent_autopay`; `jobs`: the delete kinds take a rent charge; runners), tests. No
migration, no new dependency or environment variable; provider calls are the existing delete,
create and list. TODO-VERIFY-064 recorded (Stax runs a new `DTSTART` without catch-up).

**Commands run (final Batch C code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 477 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 386 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 1 | 5,133 passed, 621 skipped, 1 failed: the route classification test did not list the new `retry_release` route (test list updated by Fable; the file re-run green on both engines); 7:03 wall |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 23 passed; 5:32 wall |
| the "Unit tests" selection | PostgreSQL 16 | 1 | 5,728 passed, 3 skipped; the same one failure; 9:56 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `af0e63c` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_installments.py` (the ACH split with the move's calls, rows,
audits and the three one-occurrence schedules; twice-monthly; a failure between delete and create
with Retry; the pending month refused; lease-end blockers after a move and Stop autopay; the
rent-alone invoice released; the mixed invoice and the invoice-plus-autopay cases refused; the wire
lease unchanged), `test_lease_services.py` (+7 `move_autopay`/`debit_instant`),
`test_attention.py` (the rent parent's item). Fable (`test_plan012b_b_c_adversarial.py`, 4
cases): the moved schedule starts next month, never later, with the audit and the three
installment schedules after it; a release on another lease's rent charge, or with the other
lease's schedule in its parameters, deletes nothing of that lease; a late debit of the old
schedule lands on the rent charge only, the plan goes to attention with its item, the
installments stay owed and nothing is voided; a lease end after the move sees only the new
schedule as live and Stop autopay clears it (both schedules deleted at the fake).

**Findings (C).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 4 | MAJOR | `attention._split_charge_paid` | The "payment on a split charge" item covered one-off parents only; a rent parent's plan went to `attention` after a late autopay debit with no item on the list | Resolved: a second query for rent parents (stored `amount_paid`, consistent with `parent_paid_on_plan`), the rent label, the lease's operator for the filter; tested |

Design choices accepted: each debit of a twice-monthly lease keeps its own start point (only
the split charge's debit skips a month); the retry is tracked per plan in the audit, never
deletes twice and never recreates a schedule staff stopped meanwhile; a rent charge with both
its own live invoice and autopay ahead stays refused; a release job that finds a pending payment
fails with the pending sentence.

**Residual risk.** Stax's handling of the new `DTSTART` (TODO-VERIFY-064; the owner's first
autopay split on staging settles it); a retry after next month's debit date would send a past
`DTSTART`, which Stax refuses, visible as a failed job; the move runs under the lease lock, its
race against a concurrent Stop autopay is reasoned, not tested.

**Verdict (C):** PASS (one MAJOR resolved in one rework cycle; the classification list was a test-list omission).

**Confidentiality gate (§5.4).** Secret scans clean on every gate; no secret-store read by either
model; no new credential-capable file type; no new secret name.

**Verdict:** PASS (Batches A, B and C; three MAJOR found in the gates and resolved; no finding deferred; residual risk as listed per batch, TODO-VERIFY-064 open until the first autopay split on staging).

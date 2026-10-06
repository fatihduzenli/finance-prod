# PLAN-011 — Dashboard, month grid, reports, exports, mobile and PWA; waive fix

**Date:** 2026-10-06 · **Author:** Fable (QA owner) · **Implementer:** Opus
**Origin:** Prompt 11 with the owner's seven amendments (2026-10-06), and the owner's 10d finding
on Waive. Pure application work: no Stax call is added, no infrastructure change.

## 1. Restated objective

The test of this step is whether the owner can stop opening the spreadsheet. The staff screen
becomes the **month grid** the Rents tab had: stores down, months across, one coloured cell per
rent charge with a note marker and a click-through, filtered by operator, state, channel and
status. Around it: a dashboard that answers "who owes what, and since when" above the fold, an
arrears list by months behind including one-off charges, today's run summary, and the reports
Step 11 names: A/R aging, revenue by month, payments by method, deposits versus transactions, a
printable customer statement per operator, plus the P&L already built. Every list and report
exports CSV with headers an accountant can read. Every amount comes from stored exact decimals.
Every page works on a phone, the app installs to the home screen through a manifest and a
service worker that caches static assets only, documents can be captured from the camera, and
MFA works on the phone. A fixture reproduces August 2026 (392 collected, $1,470,001.57, 47
unpaid, 13 pending) until Step 13 provides the migrated data. First, the Waive defect: waiving a
charge must never leave a live invoice at Stax.

## 2. Affected packages

`backend/ledger` (new `reports`, `dashboard`, `grid` services and views, templates, CSV, indexes
migration, one-off charge waive), `backend/finance` (home URL, login redirect, static assets:
manifest, service worker, icons, CSS), `backend/accounts/registry.py`, `backend/tests`, `docs/`.
No new dependency. **Out of scope:** the Google Sheet push (amendment 7: optional, off; not
built), reminders and e-mail (Phase 7), NSF fees and installments (11b, 12b), Step 13's import.

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D0 | **Waive and a live provider invoice** (owner finding). `waive` on a one-off charge that has a provider invoice reference and is not yet deleted at Stax **refuses** with "This charge has a live Stax invoice. Cancel it first (Stax deletes the invoice), then waive if anything is still owed." A charge with no provider invoice, or whose invoice already reads `DELETED`/`void` locally, waives as today. The page shows the two actions with one line each: **Cancel** = "removes the Stax invoice and closes the charge, nothing owed"; **Waive** = "closes a charge that has no Stax invoice without collecting it". **Remedy for existing rows:** a new admin action **Delete provider invoice** on a waived charge whose invoice is still live (`one_off_charges:delete_invoice`, job `invoice_delete` as Cancel uses, audited `one_off_charge.waived_invoice_deleted`), so the owner clears `INV-2026-0004`; the dashboard lists such charges under "Waived with a live Stax invoice" until none remain | Owner finding of 2026-10-06 |
| D1 | **Home page `/`** = the dashboard (every role; `LOGIN_REDIRECT_URL` → `/`), above the fold: total outstanding (sum of `balance` over rent, insurance and non-refundable one-off charges not waived or voided); total overdue with count (due date before today, balance > 0); paid this month (settled payments by `settled_on` in the current month, net of settled refunds, excluding deposits); invoices created but not sent (`internal_status` in draft/ready with a provider ref); failed payment attempts (invoices whose `provider_status` is `ATTEMPTED`, with operator and store, the most visible list); today's run summary (due, pending, returned counts with links to the run and returns pages); last reconciliation (status, started, link); "Waived with a live Stax invoice" (D0) while any exist. Each figure links to the list that produces it | Step 11 item 1; amendment 3 |
| D2 | **Month grid `/grid/`** (every role; the primary staff screen; nav first): rows = stores (label, number, operator, channel), columns = months of a range (default the current month and the 11 before; `from`/`to` parameters, cap 24), one cell per rent charge: colour by status (`paid` green, `unpaid` red, `pending` amber, `partial` orange, `due` grey-blue "due", `waived` grey, no charge blank, "not due" for months before the lease start), the amount, a note marker (●) when the charge has a note, a click-through to the charge page; filters by operator, state, channel and status (status filter keeps rows with at least one matching cell); sticky first column and header; keyboard reachable. **CSV export** in the sheet's column order (`docs/COLUMN_MAPPING.md`: store number, store, state, operator, channel, then one column per month with the cell text) so staff can diff it against the spreadsheet | Amendment 1; SCOPE Flow 3 and 4 |
| D3 | **Arrears `/arrears/`**: one row per operator across stores: months behind (count of unpaid or partial rent charges with due date before today) bucketed 1, 2, 3+; total owed = rent balances + insurance balances + non-refundable one-off balances (not waived/voided); last payment date; channel(s); the stores involved; sortable; filters by operator, state, bucket; totals per bucket and overall; CSV | Amendment 2; SCOPE Flow 3 |
| D4 | **Reports menu** (every role; `/reports/`): A/R aging (`/reports/aging/`: per operator, buckets current, 1–30, 31–60, 61–90, 90+ by days past due, over the same charge set as D1's outstanding, with totals; the report's grand total **equals the dashboard's total outstanding** by construction: both call one function `receivables.open_balances(today)`); revenue by month (`/reports/revenue/`: billed and collected per month, rent, insurance, fees, from the P&L's row engine; deposits excluded); payments by method (`/reports/payments-by-method/`: settled payments per month by our methods ACH, Zelle, wire, other; cards do not exist in this app and cash/check appear only as the Stax mark of a manual payment, shown as a note); deposits versus transactions (`/reports/deposits/`: deposits received (the `deposits` table and reconciliation step c) against settled ACH payments, unmatched items highlighted from `reconciliation_mismatches` kinds `deposit_unmatched` and `settled_payment_undeposited`); customer statement (`/operators/<id>/statement/`: printable, per operator, all stores: open charges with due dates and balances, payment history for a date range, totals; a print stylesheet; CSV); the P&L already in the menu. Every report takes a date range with the P&L's bounds and the statement timeout | Step 11 item 2; amendment 4 |
| D5 | **CSV everywhere**: every list page (operators, stores, leases, charges, payments (exists), one-off charges, returns, webhooks, documents list, reconciliation mismatches, audit (exists)) and every report has an Export CSV of the filtered rows; one shared writer (`ledger/services/csv_export.py`: the formula-safe cell, money as `0.00`, dates ISO, header names in plain English with units, e.g. `Amount due (USD)`, `Due date`, `Operator`, `Store number`); streaming; a row cap of 50,000 with a final "truncated" row; a header-name test that every exported column name is in a reviewed list | Step 11 item 3 |
| D6 | **Performance**: the grid, arrears, aging and dashboard run from a handful of grouped queries (no per-row queries; `CaptureQueriesContext` bound) and finish under 2 s on PostgreSQL with 10,000 invoices and 12,000 charges (a fixture builds them); new indexes, each justified: `rent_charges (lease_id, period_month)`, `rent_charges (status, due_on)`, `payments (settled_on, status)`, `invoices (provider_status)`, `one_off_charges (operator_id, status)`, `insurance_charges (lease_id, due_on)`; added by one migration; the QA report lists them | Step 11 item 5 |
| D7 | **Exact money**: every total is a `Sum` over `numeric(12,2)` columns or a `Decimal` sum in Python; a test greps the new modules for `float(` and for arithmetic on floats; CSV writes `Decimal` as text | Step 11 item 6 |
| D8 | **Baseline fixture**: `tests/fixtures/baseline_aug_2026.py` builds leases and charges for August 2026 that total 392 collected for $1,470,001.57, 47 unpaid, 13 pending (amounts spread deterministically, not uniform); tests assert the grid's August column totals, the arrears count, the dashboard's figures and the run summary against them; after Step 13 the QA report records the same check on the migrated data | Amendment 5 |
| D9 | **Mobile and PWA**: a responsive layout for every template (the base CSS gains breakpoints; tables get a `data-label` per cell and collapse to cards under 720 px, or scroll horizontally for the grid and the audit page with the first column sticky; touch targets ≥ 44 px; forms single-column); `manifest.webmanifest` (name, short name, icons 192 and 512 generated as PNGs, `display: standalone`, theme colour; served by whitenoise), a service worker at `/sw.js` that precaches **static assets only** (the hashed static manifest) and never caches any `/` response, API or HTML (network-only for everything else; a test loads the worker source and asserts the allow-list), registered from the base template; the sandbox banner stays visible and fixed on mobile; document upload inputs gain `accept="image/*,.pdf,.docx,.heic"` and a second "Take photo" input with `capture="environment"` (images only); the MFA pages (django-two-factor) are checked on a 390 px viewport in Playwright (login, token, redirect to `/`, open the grid); no offline mode | Amendment 6 |
| D10 | **Roles**: dashboard, grid, arrears, reports, statement and every CSV are read routes (viewer and above); the D0 remedy action is admin. Nothing here writes to Stax | Pattern |
| D11 | **Google Sheet push**: not built; the feature flag and the one-way rule are recorded as a Phase 7 item | Amendment 7 |

## 4. Interface contract

Services: `ledger/services/receivables.py` (`open_balances(today) -> list[OpenBalance]`, `aging(today)`,
`arrears(today, filters)`, `dashboard(today)`), `ledger/services/grid.py` (`GridQuery`, `rows`, `csv_lines`),
`ledger/services/reports.py` (`revenue_by_month`, `payments_by_method`, `deposits_vs_transactions`,
`statement(operator, range)`), `ledger/services/csv_export.py` (`CsvWriter`, `HEADERS`), `one_off_charges.waive`
refusal + `delete_waived_invoice`. Views/URLs: `ledger:home`, `ledger:grid:page|export`, `ledger:arrears:page|export`,
`ledger:reports:index|aging|revenue|payments_by_method|deposits|…_export`, `ledger:operators:statement|statement_export`,
`ledger:one_off_charges:delete_invoice`, `finance:manifest`, `finance:service_worker`, plus `export` routes on
every list. Static: `finance/static/finance/{app.css,sw.js,manifest.webmanifest,icons/*}`. Migration: indexes only.

## 5. Work breakdown

| Batch | Task | Done when |
|---|---|---|
| A | D0 waive fix and remedy; D1 dashboard as home; D2 month grid with filters and CSV; D3 arrears; D8 baseline fixture and its tests; D6 indexes migration; tests | The fixture's August column shows 392 green, 47 red, 13 amber and the right totals; a waive on a live invoice is refused; the remedy deletes the invoice through the worker |
| B | D4 reports and statement; D5 CSV on every list and report with the reviewed header list; D7 float test; performance fixture (10,000 invoices) and bounds; tests | Aging total equals the dashboard's outstanding on the fixture and on random data (property test); every export has readable headers; under 2 s |
| C | D9 mobile, manifest, service worker, camera capture, MFA on a phone viewport (Playwright); tests | Playwright at 390 px: login with MFA, land on `/`, open the grid, scroll it, open a charge; the worker caches only static paths; the banner stays |
| Docs (Fable) | RUNBOOK §14 (dashboard, grid, arrears, reports, exports, installing the app on a phone, the waive/cancel rule); DATA_OWNERSHIP (indexes, no new owner); DECISIONS note (no ADR needed unless a report rule is decided; D0 is recorded as ADR-0009 addendum); README; QA-011 | |

A → B → C in sequence (B builds on A's receivables; C restyles A's and B's templates).

## 6. Test plan (before implementation)

Happy paths: dashboard figures on the fixture; grid colours, note marker, click-through, each
filter, the CSV's column order against `COLUMN_MAPPING.md`; arrears buckets and totals; aging
buckets at 0, 1, 30, 31, 60, 61, 90, 91 days; revenue and payments by method per month; deposits
report with an unmatched item highlighted; statement for an operator with two stores; every
export opens as CSV with the reviewed headers; manifest and service worker served with the right
content types; camera input present. Boundaries: a month with no charges; a store with no lease;
a lease that started mid-range; a charge paid in a later month than due (aging by due date,
revenue by settled month); a partial payment; a waived rent charge (grey, not outstanding); a
voided one-off charge (nowhere); the range cap; day-of-month 31; a payment settled on the last
day of the month at 23:59 New York. Error paths: bad ranges and filters → 200 with a message;
statement for an archived operator; export over the row cap. Adversarial (Fable): the aging
total equals the dashboard total under random data (property test, 200 rounds); grid CSV cells
starting with `=`, `+`, `-`, `@` are formula-safe (a store label `=HYPERLINK`); a viewer
cannot reach the D0 remedy; the service worker never caches `/`, `/healthz`, `/grid/`, any
`/reports/` or any response with `Cache-Control: private`; the manifest carries no secret and no
internal hostname; the 10,000-invoice fixture under 2 s with a bounded query count; the grid
with 500 stores × 24 months renders under 3 s; filters with SQL-ish text; the statement for an
operator never shows another operator's charges; a `Decimal` round-trip through every CSV
(`0.10 + 0.20 == 0.30`); the MFA login on a 390 px viewport with the keyboard open (Playwright
mobile emulation) reaches the grid; the sandbox banner is within the viewport on load.

## 7. Risk register

- **Two reports, two totals**: only one function computes open balances (D4); a property test
  guards it. Revenue (by settled month) and aging (by due date) are different questions and the
  pages say so.
- **Performance on 10,000 invoices**: grouped queries and indexes; measured, not assumed.
- **Service worker caching financial data by mistake**: an allow-list of static paths, a test
  over the worker's source, and `Cache-Control: private, no-store` on every dynamic response
  (middleware; verified).
- **Mobile MFA**: django-two-factor templates are restyled, not replaced; a Playwright run at a
  phone viewport is the proof.
- **Waive refusal breaks a habit**: the page explains both actions; the remedy clears the
  existing row.
- Irreversible: none; the indexes migration reverses.

## 8. Acceptance criteria

Gate green on both engines; migrations clean; the aging grand total equals the dashboard's
outstanding on the fixture and under the property test; the August 2026 fixture reproduces 392 /
$1,470,001.57 / 47 / 13 on the grid, the arrears page and the dashboard; the 10,000-invoice
fixture runs each report under 2 s; on staging, on a laptop and a phone: the grid shows the test
stores with the right colours and opens a charge; arrears and today's run match the run page; CSV
exports download; the app installs to the phone's home screen and the owner logs in with MFA and
opens the grid there; Waive on a charge with a live invoice is refused and the remedy clears
`INV-2026-0004`.

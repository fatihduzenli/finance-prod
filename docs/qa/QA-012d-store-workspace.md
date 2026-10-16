## QA Report — PLAN-012d: the store page as the single workspace

**Date:** 2026-10-15 · **QA owner:** Fable · **Implementer:** Opus · **Plan:**
`docs/qa/PLAN-012d-store-workspace.md` · **Decision:** ADR-0013 (proposed)

### Batch A — the store workspace, the lease page, links and navigation, drafts' actions

**Scope.** `backend/ledger` (new `services/workspace.py`, `services/credits.py` (stub for Batch
B), `views/store_context.py`; `views/stores.py` rewritten detail plus `store_record_payment`
(Batch A: opens the Charges tab) and `store_charge_now_pick`; `views/invoices.py` `draft_delete`,
the store context and the "already on draft" link; `views/leases.py` (create/edit read the
context; the detail page without its lists); `views/one_off_charges.py`, `charges.py`,
`contacts.py`, `documents.py`, `installments.py`, `payments.py` (context, returns, crumbs);
`services/audit.py` `rows_for_store`, `store_of=`; `services/contacts.py` `open_promise`;
`services/attention.py` store-tab urls; `services/invoice_builder.py` `delete_draft`; templates:
the store page split into header and tab partials, `charge_now_pick.html`, crumbs, the context
line; `finance/templates/finance/base.html` (Rent run out of the nav, an Admin menu,
`aria-current`), `finance/context_processors.py` (registered in settings), `app.css`; `urls.py`,
`accounts/registry.py`), tests. No migration, no dependency, no environment variable, no provider
call.

**Decisions in the gate.** (1) "Schedule a payment" left Batch A: the setup-charge form cannot
schedule a debit; Batch B adds `stores:schedule_payment` (plan amended). (2) The Charges tab's
insurance balance uses the receivables figure so the tab and the header agree. (3) Accepted:
the Charges tab lists every lease of the store (old leases' charges stay reachable); Resolve
deposit moved to the Charges tab rows; `store_cost` rows in History; the nav markup kept.

**Commands run (final Batch A code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 485 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 392 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 5,178 passed, 621 skipped; 12:10 wall (a slower machine this run) |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 23 passed; 8:55 wall |
| the "Unit tests" selection | PostgreSQL 16 | 0 | 5,773 passed, 3 skipped; 15:04 wall (the first gate of this batch failed the revenue query budget, finding 4; the whole gate was run again after the fix) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `dce9187` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_store_workspace.py` (35: the header figures and the vacant bar,
every action's target and role, hidden fields, read-only line and return tab, `next` and 404
checks, the tabs' filters, Send and Delete with refusals, Schedules, Contacts, Documents,
History, a query-budget test per tab, the lease page, crumbs, attention urls, run links, nav and
`aria-current`, the "already on draft" link); existing tests updated for the moved sections.
Fable (`test_plan012d_a_adversarial.py`, 5 cases): a form opened from a store never pre-fills a
lease of another store (four forms); `next` cannot leave the site after a save; a viewer sees no
bar, gets 403 on every bar action, sees no History and anonymous gets 401; the History tab never
shows another store's rows; the drafts' Delete cannot delete a sent invoice, GET never deletes,
a viewer cannot delete. One older adversarial case (PLAN-012c-b, the closing entry on the store
page) now reads the Contact log tab.

**Findings (A).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 1 | decision | plan D2 | "Schedule a payment" had no form behind it (the setup-charge form makes `none`-collection charges only) | Resolved: the action moves to Batch B with its own route |
| 2 | MINOR | Charges tab | The insurance balance ignored re-debits, unlike receivables | Resolved: the receivables figure is shared |

| 4 | MAJOR | `accounts/roles.role_of` | One groups query per call; the nav's new `is_billing` beside `is_admin` added a query to every page, and the revenue report broke its budget (31 of 30) in the volume test on PostgreSQL | Resolved: the role is resolved once per user object (one groups query per request, shared by the middleware, the views and the template filters; deactivation still takes effect at once); the revenue page fell from 31 to 29 queries, every page by two; budgets unchanged; tests for one query per object |
| 3 | MINOR | Charges tab | Rent and one-off rows read the stored balance, so a rescheduled return's carried amount (which receivables subtracts) can make the tab total exceed the header; a carried insurance charge reads "Paid" | **Deferred** (written decision: the header is the figure of record; the rows follow the stored balance as the charge pages do; a "Carried by re-debit" label and the carried deduction on rent and one-off rows go to Batch D with the other operations-page work, finding 12d-2) |

Design choices accepted: the Charges tab lists every lease of the store; Resolve deposit on the
rows; `store_cost` rows in History; the nav markup kept; "Record a payment" opens the Charges tab
until Batch B; the Admin menu as a `<details>` element.

**Verdict (A):** PASS (two rework cycles: the scheduled-payment action moved to Batch B and the insurance figure shared; then the per-page role query; one MINOR deferred by written decision).

### Batch B — Record a payment with manual allocation, lease credits, Schedule a payment (ADR-0013)

**Scope.** `backend/ledger` (new `services/payments_entry.py` (`open_charges`, `record`,
`schedule`), `services/credits.py` (`balance`, `held`, `create`, `apply`, `refund_all`,
`rows_for_store`), `services/insurance.py` `record_manual_payment`; `rent_run`,
`one_off_charges` (`allocation_group`, `credit_id`, `payment_refusal` with the credit method,
`_billed_setup` accepting autopay); `statements.py` (`CreditLine`, the PDF section),
`lease_end.py` (`_refund_credits`), `transfer.py` (`CREDIT_HELD` blocker), `workspace.py`
(Schedule a payment, credit rows), `receivables.py`, `reports.py`, `payments_ledger.py`
(credits left out of cash figures); `views/stores.py` (`store_record_payment`,
`store_schedule_payment`), `views/reports.py` (P&L footer); forms, templates,
`payment_allocation.js`; `models.py`: `PaymentMethod.CREDIT`, `PaymentKind.CREDIT_REFUND`,
`MoneyMethod`, `LeaseCredit`, `Payment.allocation_group/credit` with three CHECKs; migration
0042 with the guard trigger on `lease_credits.note/reference`), `backend/payments`
(`offline_marking` memo "Paid by credit"), `urls.py`, `accounts/registry.py`, tests. No new
dependency, environment variable or provider call.

**Commands run (final Batch B code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 489 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 396 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 5,228 passed, 630 skipped; 12:44 wall (a first attempt of this gate crawled for four hours while the machine idled and was stopped; the re-run under a keep-awake wrapper is this row) |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 24 passed; 10:20 wall |
| the "Unit tests" selection | PostgreSQL 16 | 0 | 5,831 passed, 3 skipped; 15:23 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `1de1a00` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_payment_allocation.py` (23: sums, pre-fill, keep-credit, a later
application, Stax marking when covered and the builder case, partial flagged, insurance, replay,
the P&L footer, the statement's credit line and its refund at confirm, the transfer blocker,
receivables unchanged, the scheduled payment and its refusals, the header and the Payments tab,
the seeded round trip of 0042, the form's query budget), `test_ledger_models.py` (the new
CHECKs), the guard lists. Fable (`test_plan012d_b_adversarial.py`, 7 cases, both engines): a
charge of another store (or a random id) in the allocation is refused with nothing written; a
replayed key never pays twice (one allocation group, two payments); a credit cannot be applied
beyond its remaining, in one request or across two; three seeds of random allocations with
credits kept and applied never lose or double count a cent (cash = direct + kept; remaining =
kept − applied; open balances = billed − direct − applied; P&L collected = direct + applied);
viewer 403, anonymous 401; a card number in the note or the reference is refused and reaches no
audit row.

**Findings (B).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 7 | MAJOR | `payments_entry.py`, `insurance.py` | Named the processor outside `backend/payments/` (Rule 8 fence test) | Resolved: reworded to "the provider"; the insurance notice no longer goes through the marking's `shown()` wrapper (same text) |
| 8 | MAJOR | `payments_entry.record` | The call-graph scan of `test_jobs` found the entry reaching the provider (by name through the per-charge services' marking path) and colliding with a view-side `record` | Resolved: a parameter named `provider` (the list of notices) had made the scan treat the entry as reaching the provider; renamed, the entry renamed `record_entry`, the rent deferral written as a literal `defer_mark=True` call; the entry only writes rows and queues the mark jobs |
| 9 | MINOR | templates, `test_store_views` | `{% load static responsive %}` instead of the literal `{% load responsive %}` line the responsive test expects; the new `schedule_payment` route missing from the planned-route list | Resolved: separate `{% load responsive %}` line; the route added to the planned list |
| 5 | MINOR | store header | "Credit $0.00" is shown when no credit is held; the owner asked for the credit "if any" | Resolved: the Credit line shows only when a credit is held |
| 6 | MINOR | insurance manual payment | An insurance charge's provider invoice is not marked paid as cash (no insurance mark job exists); the charge is flagged `needs_review` and audited `provider_not_updated` | **Deferred** (written decision: insurance invoices come from the yearly schedule and are rarely paid by hand; a mark job for insurance is a small follow-up if the owner meets the case; the flag keeps the review visible) |

Design choices accepted: the credit refund at lease end as payment kind `credit_refund` (a
manual-method payment naming the credit, no charge, left out of the cash figures); credit-only
applications with an empty amount; the dashboard's "paid this month" without credit
applications (they move no money) while the P&L counts them as collected when applied; deposits
still owed listed on the form; one form that both allocates cash and applies credit to a charge
with a plain invoice makes two payments and flags the charge partial (today's rule); the
scheduled payment maps its item as the builder does.

**Verdict (B):** PASS (one rework cycle for five convention tests and the header's credit line; one MINOR deferred by written decision: the insurance invoice is not marked at the provider).

### Batch C — the setup checklist without tick boxes, the builder's single combo box

**Scope.** `backend/ledger` (`models.py`: `FeeType.show_in_setup`,
`InvoiceDraftLine.existing_charge` with its CHECK; migrations 0043 (with the data step) and 0044;
`admin.py` the flag; `services/one_off_charges.py` `offered_types`/`checklist_types`;
`services/invoice_builder.py`: `open_charges`, `existing_charge_closed`, `_clean_existing_line`,
`_check_existing_lines`, `rent_amount`, `_attach_existing`; `services/transfer.py`
`deposit_type`; `forms.py`: the checklist without ticks and with `setup_total`, the builder line
form without `kind`, the month choices, `kept_rows`/`line_numbers`; `views/invoices.py`:
`_item_options`, `_line_rows`, `_renumber`, the `rent_amount` JSON view; `views/transfers.py`;
`urls.py`, `accounts/registry.py`; templates `_draft_form.html`, `leases/form.html`,
`transfers/incoming.html`, `lease_ends/statement.html`; static `invoice_lines.js` rewritten,
new `table_total.js`, `app.css`), tests. No new dependency, environment variable or provider
call.

**Commands run (final Batch C code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 493 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 400 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 1 | 5,251 passed, 631 skipped, 1 failed: the new draft-line CHECK had no constraint case in `test_ledger_models` (Fable added it; the file re-run green on both engines); 13:14 wall |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 26 passed; 10:59 wall |
| the "Unit tests" selection | PostgreSQL 16 | 1 | 5,853 passed, 3 skipped; the same one failure; 15:43 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `eec5d05` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_builder_lines.py` (14: one line on a new draft, the list
order, the month select, a rent line from the Item box, the endpoint's amounts and refusals and
binding, the checklist deposit attached on send with its audit, repricing by a cent, charges on
a live invoice or a plan not offered and refused, duplicates refused with the link, rent fields
or quantity 2 or an operator-level draft refused, removed lines ignored and visible numbering,
the trash and the total, the CSS rule, builder-saved items hidden, the admin flag),
`test_setup_charges.py` (the checklist rewritten: placeholder and no value, no tick, total, the
flag, only priced rows charged), `test_transfer.py` and `test_lease_end.py` (the same table
rules), seeded round trips of 0043 and 0044 on both engines. Fable (`test_plan012d_c_adversarial.py`,
5 cases, both engines): the rent-amount endpoint refuses another operator's lease (404), an
unknown lease, a bad month, a viewer (403) and anonymous (401); an existing charge of another
operator or of another lease of the same operator cannot be attached and nothing is saved; an
existing-charge line's amount cannot exceed its balance (nor be zero or negative); a removed
line with hostile values never blocks the save; a posted tick field charges nothing and the
placeholder is never a filled value.

**Findings (C).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 10 | decision | `_attach_existing` | Billing an existing charge below its balance reprices the charge to the line amount (a $500 deposit billed at $450 becomes a $450 deposit), audited | **For the owner**: accepted for the gate as the only rule that keeps the invoice total equal to the billed total; the owner confirms or asks for a refusal below the balance |

Design choices accepted: attaching sets the charge's collection to the invoice's (a checklist
deposit moves from `none` to e-mail); legacy charges, companion fees and charges that ever had a
plan are never offered; an operator-level draft offers no existing charges; without JavaScript a
new draft has one row; months older than twelve back cannot be billed from the page; "Rent" is a
sentinel the Item box reserves.

**Verdict (C):** PASS (no rework cycle; one test-list omission filled by Fable; one decision for the owner: repricing an existing charge billed below its balance).

### Batch D — the run page's list and the tick's row, the grid window, Create missing charges, finding 12d-2

**Scope.** `backend/ledger` (`services/rent_run.py`: `leases_due(store=)`, `_run`,
`catch_up_store`, `record_run`, `last_run`, `run_line`, `created_message`;
`management/commands/collections_tick.py` records the run; `views/run.py`, `templates/run/today.html`
(the last run and `#created-charges`); `views/dashboard.py`, `dashboard/home.html` (`#run-line`);
`services/grid.py` (`default_range` 10 + 1 + 1, `picker_bounds`), `grid/page.html` (month selects);
`views/stores.py` `store_create_charges`; `urls.py`, `accounts/registry.py`;
`services/workspace.py` (`Action.post`, "Create missing charges", `store_balances`, the Charges
tab rows on the receivables figures with "Carried by re-debit"); `services/receivables.py`
`open_balances_and_carried`; `stores/detail.html` POST actions as forms), tests. No migration (the
run's report is an audit row `rent_run.ticked`), no dependency, no environment variable, no
provider call.

**Commands run (final Batch D code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 495 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 402 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 5,272 passed, 631 skipped; 13:15 wall |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 26 passed; 11:05 wall |
| the "Unit tests" selection | PostgreSQL 16 | 0 | 5,874 passed, 3 skipped; 15:45 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `ca3c2b9` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_operations_pages.py` (14: the button and the tick both list
what they created with store and charge links; the run row and its actor; the dashboard line;
Create missing charges with catch-up, today's debit and an ended lease, the store scope, the
audit rows, "Nothing missing", a paused lease named, the bar form, 404s; the grid's default
window and the pickers, the swap, the cap, hostile values not echoed; a future month billed in
advance with the invoice number; "not due" only before the start; 12d-2 with rent and one-off
returns equal to the header), `test_grid.py`, `test_store_views.py`, `test_store_workspace.py`
updated. Fable (`test_plan012d_d_adversarial.py`, 3 cases): Create missing charges on one store
never touches another, is refused to a viewer (403) and anonymous (401), creates the three
missing months once and says "Nothing missing" on a second click, and GET is not an action; the
grid's default window is December 2025 to November 2026 in the table's own headers, a reversed
range is swapped, a range wider than 24 months is capped to its latest 24, and hostile values fall
back to the default window without being echoed and without adding a script to the page; the
run page's created-charges table names only what it just created, with store links and the
catch-up marker.

**Findings (D).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 11 | MAJOR | grid pickers | From/To were HTML month inputs; the owner's text rules those out for Safari (item 8b's rule, the same staff and browsers) | Resolved: From and To are selects of the current month ± 24 (the shown range's months added), no JavaScript; the reports pages' own month inputs go to Batch E under the same rule |

Design choices accepted: the button writes the same `rent_run.ticked` row as the tick, with its
user as actor (the run page shows the latest of either; the dashboard sums today's rows); Create
missing charges writes no run row (it is audited per charge); 12d-2 covers open rent and one-off
rows (unpaid deposits and parents on a live plan keep the stored balance as before).

**Verdict (D):** PASS (one rework cycle: the grid pickers as month selects; finding 12d-2 resolved).

### Batch E — the operator page as the workspace; month selects on the reports

**Scope.** `backend/ledger` (`services/workspace.py`: `Owner`, `_owned`, the tab builders
by owner, `operator_workspace`, `_selected`, `_operator_lease`, `_operator_actions`,
`operator_tab_url`; `services/credits.py` `rows_for_operator`; `services/audit.py`
`rows_for_store(operator=)`; `services/grid.py` `picker_months` over any month range;
`views/operators.py` the detail page on the workspace; `views/invoices.py` the drafts' `next`;
`views/reports.py` the selects; templates `operators/detail.html` (the workspace section with
the store selector, the operator-level items kept above), `stores/_header.html`, the tab
partials with a store column, new `ledger/_month_select.html` used by the grid, the ranged
reports and the P&L), tests. No migration, no new route, no dependency, no environment variable,
no provider call.

**Commands run (final Batch E code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 497 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 404 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 5,304 passed, 631 skipped; 7:58 wall |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 26 passed; 6:54 wall |
| the "Unit tests" selection | PostgreSQL 16 | 0 | 5,906 passed, 3 skipped; 10:47 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `2597518` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_operator_workspace.py` (29: the selector's default, the
`?store=` switch with the bar's lease, another operator's store or a bad value ignored, a store
now leased to another operator, an operator with no store, the tabs across stores with a store
column and exports, Documents and History of the selected store, the drafts' Edit, Send and
Delete with `next` and an off-site `next` ignored, the operator-level items kept and the old
lists gone, the roles, the query budget (at most 30 for three stores, constant as data grows), the
reports' selects and the P&L export's range), three existing tests moved to the Invoices tab.
Fable (`test_plan012d_e_adversarial.py`, 3 cases, both engines): the selector cannot pick another
operator's store (neither its figures nor its lease reach the page); the bar's actions carry
only the selected store's lease and a viewer sees none; the operator page's drafts list has no
Delete for a sent invoice and the route refuses it.

**Findings (E).**

No finding. Design choices accepted: forms opened from the operator page's bar return to the
store page's tab (the store context takes priority), the per-charge payment forms and the
drafts' Send and Delete return to the operator page; the selector lists every store the operator
ever leased and shows "No current lease of this operator" without a bar when the store is now
another operator's; the Contact log tab points to the operator-level log above instead of a
second copy; the store tab partials keep their table ids on the operator page.

**Verdict (E):** PASS (no rework cycle, no finding).

**Confidentiality gate (§5.4).** Secret scans clean on every gate; no secret-store read by either
model; no new credential-capable file type; no new secret name.

**Residual risk (whole step).** The JavaScript of the payment-allocation form (live remainder,
single-charge pre-fill), the builder lines (combo, month select, amount fetch, trash) and the
table totals ran only in unit tests of the server side; the owner's clicks are the browser
check, Safari included. The insurance manual payment flags review instead of marking at the
provider (finding 6, deferred). Credits are a new liability: ADR-0013 is Proposed until the
owner confirms. An existing charge billed below its balance is repriced (finding 10, owner's
decision). The four migrations 0042–0044 are additive.

**Verdict:** PASS (Batches A–E; findings 1–11: four MAJOR and five MINOR resolved, two MINOR
deferred by written decision (3 → closed in D as 12d-2; 6 open), two decisions for the owner
(ADR-0013; the repricing of an existing charge)).

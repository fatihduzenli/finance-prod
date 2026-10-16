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

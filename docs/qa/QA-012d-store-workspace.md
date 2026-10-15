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

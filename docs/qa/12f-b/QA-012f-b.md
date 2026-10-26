# QA — Step 12f-b, the owner's staging review (findings 1–7)

Plan: `docs/qa/PLAN-012f-b-staging-review.md`. One section per batch, in order of delivery: Batch 0 (CI
and guards), A (dashboard), B (stores and operators), C (Add payment with ACH), D (Create invoice:
Save, the e-mail prompt, Schedule settlement), E (the remaining pages to the spec). Each section
records the scope, the findings with their severity and outcome, the commands run with exit codes
on both engines, the tests added, every baseline regenerated (in the Playwright image against
PostgreSQL) and the laptop and phone screenshots of every changed page under `docs/qa/12f-b/shots/`.

Each batch is its own pull request from a `step-12f-b/<batch>` branch (owner's instruction,
2026-10-25); the push, the pull request and the merge are each a publish action under the root
CLAUDE.md §6 and wait for the owner's written authorisation.

## Batch 0 — CI and guards (branch `step-12f-b/batch-0`)

**Scope (Fable alone; test files and verification config under CLAUDE.md §1.1).**
`backend/tests/test_migrations_roundtrip.py`: the module list becomes `GUARD_MODULES` with ledger
0046, 0048, 0050 and payments 0018 added, read through `guarded_columns()` (a migration that
declares no `GUARDED_COLUMNS` counts as none). `backend/tests/test_card_number_guard.py`: its list
becomes `GUARD_MODULES` with 0041, 0046, 0048, 0050 and payments 0018 added, and a new test
`test_every_migration_that_may_guard_a_column_is_in_both_guard_lists` walks both apps' migration
folders: every migration that declares `GUARDED_COLUMNS` must be in the card-number list, and every
migration from ledger 0041 and payments 0018 on must be in both lists, so a new migration left out
fails the suite. `.github/workflows/ci.yml`: the migration job's `timeout-minutes` 30 → 45 with a
comment marking it temporary (Step 12f-b adds several migrations; Step 13 owns the split or squash;
the job's time is reported at the checkpoint). The STATUS commit `871a846` (slice F deployed) rides
on this branch, as the owner asked. Docs: `docs/qa/PLAN-012f-b-staging-review.md` (the Phase A plan)
and this file's skeleton.

**Findings.** None in the code under review (no production code changed). Noted for Step 13: the
round-trip list omits ten migrations that the card-number list carries (ledger 0004, 0005, 0009,
0010, 0012, 0015, 0016, 0024; payments 0003, 0007), by design — their triggers were later replaced
or renamed — so the completeness test requires both lists only from the thresholds on.

**Gate.** ruff, `ruff format --check` (487 files), mypy strict (487 files), `uv lock --check`: clean. sqlite: full suite 7,022 passed, 674 skipped, 98 deselected in 1,412 s (the new completeness test included). PostgreSQL 16 (CI's split on a throwaway container): migration selection 36 passed in 1,392 s (the round trips, 23:12 locally), unit selection 7,657 passed, 3 skipped, 101 deselected, e2e 31 passed on each of four runs.
Visual: no page changed; not run (the CI visual job runs on the PR). Secret scans clean; no secret-
store read; no new file type; the workflow edit is one number and a comment, parsed by a YAML load.

**Verdict (0):** **PASS.** No production code; the two lists complete and guarded by a test; the job limit raised to 45 minutes and marked temporary; the Phase A plan for the step committed with it.



## Batch A — the dashboard (branch `step-12f-b/batch-a`, findings 4, 1 and 2)

**Scope.** Opus, two briefs. A-1: the `{% dialog %}` tag (`finance/templatetags/components.py`; 440
px `sm`, 640 px `lg`, a bottom sheet on phones; a server-opened dialog carries `open` and `data-
open` and returns to its `return_url`, a site path only) and `finance/static/finance/dialog.js`
(loaded from `base.html`; close by ×, Escape and backdrop; `data-dialog-open` openers; a server-
opened dialog switched to modal on load); the "Which store?" picker (`ledger/_store_picker.html`,
`ledger/static/ledger/store_picker.js`: 150 ms debounce, `textContent` only, arrow keys and Enter)
on the dashboard and the search results page; the JSON route `ledger:search_stores` (GET, billing
and above, at most ten rows, `<`, `>` and `&` written as `\u` escapes; read-only in the registry);
the search page's `intent=new_invoice` (stores only, each linking to its builder; one match jumps
there; a viewer's intent is ignored). A-2/A-3: `ledger/services/dashboard.py` (`Range` and
`parse_range`, `cashflow` from the P&L's rows, `payment_status` from the grid's statuses in one
query, `mini_calendar` from the Calendar's `items_in`), `finance/templatetags/charts.py` (`{% bars
%}`, `{% donut %}`, `{% chart_key %}`; the chart colours live there since the style sheets hold
tokens only), three card partials, `ledger/static/ledger/dashboard.js` (the range select submits; a
tap toggles a day's popover), the dashboard grid KPI · Cashflow · [attention | status] · [recent
payments | calendar, collection, coming up], the view's `?range=` and `?cal=` parsing with the
Cashflow inside the reports' statement timeout. Fable: `tests/test_plan012fb_a_adversarial.py` (28
cases), `tests/e2e/test_store_picker_12fb_a.py` (4 browser cases with QA shots), the visual suite's
`search-new-invoice` page.

**Findings.** A1-1 (MINOR, resolved): a server-opened dialog rendered only `data-open`, invisible
with scripts off; now `open` too, switched to modal on load without firing the return navigation.
A2-1 (MAJOR, resolved): the Paid box required a settlement inside the range on the rent charge
itself, so a charge paid through an installment plan, an invoice line or a credit fell into no box;
Paid is now every rent charge due in the range whose grid status is paid. A2-2 (MINOR, resolved): a
query-count pin for the dashboard (73 on sqlite, pinned at 80). Two of Fable's own expectations were
wrong (the app answers 401 signed out; the phone fixture's second store needed a lease).

**Departures from the owner's message, for the checkpoint.** The mini calendar shows the Calendar
page's kinds only (debit day, re-debit, insurance, lease end, note, reminder): the owner also named
promises and installments, but the Calendar's query has none and the owner forbade a second query.
Payment status covers rent charges (the grid's rules). A range longer than twelve months draws its
last twelve months and totals the whole range. The search results page gains the intent as a GET
parameter (no new URL).

**Gate.** ruff, `ruff format --check` (492 files), mypy strict (492 files), `uv lock --check`, `makemigrations --check`: clean. sqlite: full suite 7,123 passed, 674 skipped, 104 deselected in 880 s; one failure, the route digest pin (Fable's), re-pinned at 262 names for `ledger:search_stores` and re-run (25 passed). PostgreSQL 16: migration selection 36 passed in 839 s, unit selection 7,758 passed, 3 skipped, 107 deselected (the same route-digest pin, started before the re-pin; the file re-run on the container: 25 passed in 11.13s), e2e 35 passed on each of four runs (the four picker cases included). Visual: the dashboard's two baselines regenerated in the Playwright image against PostgreSQL and the picker page's two added (`search-new-invoice`), then the whole suite compared: 56 passed on PostgreSQL and 56 on sqlite. Shots: `docs/qa/12f-b/shots/a/`: dashboard and the picker page at 1280 and 390 (the baselines), the open picker on a laptop and as a phone sheet (from the browser test with `QA_SHOTS_DIR`); reviewed at both widths. Secret
scans clean; no secret-store read; no new file type; no provider name in the new files; the only
`fetch` is the picker's; every new GET writes nothing (adversarial); the JSON route answers 401
signed out and 403 to a viewer; markup in a store, operator or note name is data on every new
surface.

**Verdict (A):** **PASS.** Every gate clean on both engines; one MAJOR and two MINOR resolved; A3-1 deferred to Batch B's pull request by written decision; the departures listed for the checkpoint.

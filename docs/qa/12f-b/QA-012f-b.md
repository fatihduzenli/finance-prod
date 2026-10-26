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



## Batch B — stores and operators (branch `step-12f-b/batch-b`, findings 3 and 7)

**Scope.** Opus, four briefs, four commits (734eb9b, a4b5bec, 423153f, 8254a4f). B-1: New / Edit
store and New / Edit operator as 640 px dialogs over the list or the record page (a full-screen
sheet on phones), the old URLs rendering the page with the dialog open, POST-redirect-GET kept, a
refused form re-opening the dialog with the errors under the fields; a new record lands on its own
page with "… added." B-3: `StoreType` (five seeded, edited in the admin on the audited pattern, the
admin group granted view/add/change in the migration) and the grey pill on the store header, the
Stores list, the Monthly rent overview and the operator's store cards; the State select (50 states
and DC by name, codes stored; migration 0051 maps the free text, keeps an audit row for what it
could not map; "State needs choosing" in Needs review); landlord rent optional (empty writes no cost
row: unknown, never zero; "Not entered" on the store page). B-2: `Operator.kind` with company,
contact person, first and last name (migration 0052, the operators trigger recreated over the three
text columns); the display name derived on save and kept for existing operators, who become Person
with empty names; the search matches first and last names. B-4: the USPS address check
(`finance/usps.py`, a bounded httpx client with no retry; `USPS_CLIENT_ID` / `USPS_CLIENT_SECRET`
optional as a pair in `env_guard`, redacted from logs and Sentry; `ledger:stores:check_address`,
POST, billing, JSON; "Use this / Keep mine"; the form saves without it); the Type pair as a
segmented control; the transfer page's company shown once. Fable:
`tests/test_plan012fb_b_adversarial.py` (34 cases), the route and choice digests re-pinned (268
routes; `OperatorKind`), the Batch A MINOR A3-1 closed here (the phone chart's labels).

**Findings.** B1-1 (MAJOR, resolved): a new operator or store landed on the list, hiding the
operator's sync state and the store's setup checklist; both land on the new record's page (a
departure from "the list behind it refreshes in place", for the checkpoint). B1-2 (MINOR, resolved):
the form dialogs are full-screen on phones. B3-1 (MAJOR, pre-existing, resolved): Django's default
refusal of a wrong select value repeated the posted value in the page, so a posted card number came
back on the store, lease and calendar forms (Fable's probe); every select in the forms module now
refuses with one fixed sentence, with a test that posts a card number to every form. B2-1 / B2-2 /
B2-3 (MINOR, resolved): the transfer page's "Acme (Acme)", the unstyled Type pair, the other kind's
names kept on a scripts-off save. Opus's own finding on B-3 (the state select's refusal echoed the
value) was fixed in the slice. A3-1 from Batch A (MINOR, resolved here).

**Departures from the owner's message, for the checkpoint.** New records land on their own page, not
the list (B1-1). The full state name is shown wherever a store's state is printed, including the
address line. The USPS token's scope and the type of `expires_in` are unconfirmed without
credentials (TODO-VERIFY-065); no real USPS call was made. The provider still receives the display
name as the customer's first name (the PLAN-006 contract), not the new first and last fields. Five
admin routes and one JSON route were added (the owner's "no new URLs" cannot hold with a registered
admin list).

**Gate.** ruff, `ruff format --check` (501 files), mypy strict (501 files), `uv lock --check`, `makemigrations --check` after 0051 and 0052: clean; the pattern scan's only hits are the two route digests. sqlite: full suite 7,381 passed, 690 skipped, 104 deselected in 951 s; three pins moved by design and re-pinned by Fable (the full state name in the address line, the empty landlord rent allowed, the address-check route POST-only), their files re-run: 45 passed. PostgreSQL 16: migration selection 36 passed in 902 s (the round trips of 0051 and 0052 included), unit selection 8,031 passed, 3 skipped, 107 deselected with four failures: the three pins (re-run on the container after the re-pin, 70 passed) and the grid's query budget (20 queries for 19: the new state filter's distinct-states query, finding B3-2 below; after the fix the budget test and the grid's file passed on the container, 50 passed); e2e: the operator double-click test could not click the Type radio, which the segmented control keeps unseen by design, so the test clicks its label (7265b9e); the shell's sidebar test failed once in four runs with the machine at a load average of 19 and passed on every re-run; after the fix the full browser suite passed twice on the container (35 and 35). Visual: every baseline regenerated in the Playwright image against PostgreSQL and the suite compared on PostgreSQL (56 passed) and on sqlite (56 passed); twenty-four baselines changed: dashboard (the phone chart's labels), grid, stores, the six store tabs (the state name in the header), new-store and operator-form (the dialogs), search-new-invoice (the state name) at both widths; the other thirty-two unchanged. Shots: `docs/qa/12f-b/shots/b/`: the twenty-four changed baselines (1280 and 390), reviewed at both widths; one pre-existing copy nit noted for Batch E (the Status select's blank label reads "---------"). Secret
scans clean over the diff and the new files; `.env.example` names the two USPS variables with no
values; no secret-store read; no credential value anywhere (placeholders only in the tests); the
USPS module logs the host, status and latency only; no provider name in the new files; every new GET
writes nothing; the check route refuses GET (405), a viewer (403) and no session (401); a card
number posted to any form never comes back.

**Verdict (B):** **PASS.** Every gate clean on both engines after the re-pins and B3-2; two MAJOR and six MINOR resolved; the departures listed for the checkpoint; residual: the shell's sidebar browser test failed once in four runs on a saturated machine and passed on every re-run (recorded, not reproduced); no real USPS call (TODO-VERIFY-065); PostgreSQL ran the migrations' round trips but not the USPS client against the live service.



## Batch C — Add payment with ACH (branch `step-12f-b/batch-c`, finding 5)

**Scope.** Opus, two briefs and one rework (commits 0c9dd26, e0c426a, 02cce3e, rebased onto Batch
B). C-1, the services: `one_off_charges.bank_debit_options` (local data only — the linked bank
method's type, last four and nickname, the open balance, the large-debit flag, one plain reason when
the card is not offered) and `one_off_charges.debit` (today through the existing Charge now path
with its confirmation, audit and one-time key; a later date as a single-occurrence schedule through
the existing schedule job; the whole balance only; a replayed key writes nothing; no local payment
status written); `PaymentMethod.CASH` and `MoneyMethod.CASH` with migration 0053 rebuilding the
CHECK constraints (the payments-by-method report gains its column). C-2, the page: one template for
the one-off and the rent routes on the builder's frame (the charge in the header with its badge and
one muted line; "How is this being paid?" with two radio cards; the Note card optional on a debit,
required otherwise; the sticky summary with its tiles, the "After this payment" line and the primary
whose label follows the choice); a debit today redirects to the existing Charge now confirmation
page carrying the key; a later date schedules from the page; billing schedules from tomorrow ("A
debit today needs an admin."); the debit's note goes to the audit row. Fable:
`tests/test_plan012fb_c_adversarial.py` (19 cases), the visual suite's `add-payment` page.

**Findings.** C2-1 (MINOR, resolved): billing saw the debit card greyed although the service lets
billing schedule a later date; billing now schedules from tomorrow. C2-2 (MINOR, resolved): the note
typed on the debit card was checked and dropped; it travels to the audit row of the request or the
schedule, never in a URL. C2-4 (MINOR, deferred to Batch D's pull request by written decision): with the amount still empty the primary reads "Record $0.00 as paid"; the label should drop the figure until an amount is typed. C2-3 (MINOR, deferred to Step 13 by written decision): a debit-today note
waits in the session for the confirmation page and is removed only on success; an abandoned
confirmation leaves it there until the session ends (a plain note that already passed the card-
number check). Opus's own finding on C-1: the payments-by-method CSV's hard-coded header shifted
when Cash was added; fixed in the slice.

**Departures from the owner's message, for the checkpoint (plan D14 and §9).** A rent charge gets no
bank debit from this page: a re-debit cannot exist without a returned source payment (a database
rule) and `redebit` is outside Charge now's reach, so the card is greyed with "Rent is collected by
the lease's autopay; a returned debit is re-debited from Returned payments." — a schema change (a
nullable link to the rent charge, the CHECK relaxed) is the owner's call. No "Stax link to add a
bank account" exists (the owner's 2026-09-29 decision keeps operators off any processor page): the
greyed card says to add the account in the processor's dashboard and update. A debit takes the whole
balance on both paths (the pay call and the schedule bill the price). A later date needs a charge
with no invoice yet (a setup charge); an e-mailed charge is refused with that reason. The debit
today runs through the existing confirmation page rather than from the Add payment button (Rule 2's
confirmation step). Cash was added; Other stays. The store-level "Record a payment" page (the
allocation across charges) keeps its frame.

**Gate.** ruff, `ruff format --check` (505 files), mypy strict (505 files, after one leftover assertion of Fable's was removed, 19ccc24), `uv lock --check`, `makemigrations --check` after 0053: clean. sqlite: full suite 7,453 passed, 690 skipped, 106 deselected in 1,679 s; nine pins moved by design and re-pinned by Fable (e4777b6: Cash in the stored choices and the manual methods on four pins, the builder's method list, the payments-by-method dict, the Add payment form's required arguments in the card-number sweep, and the charge-now confirmation's audit rows, which now carry the Add payment path's `debit_requested` row beside `charge_now_requested` — a known, intended difference from the builder's Charge now), their files re-run: 324 passed. PostgreSQL 16: migration selection 36 passed in 1,598 s (26:37 under the day's load; the round trips of 0053 included), unit selection 8,104 passed, 3 skipped, 109 deselected with the same nine pins (the pipeline started before the re-pin), the nine files and Fable's re-run on the container: 343 passed in 105.45s (0:01:45); e2e 35 passed on each of four runs. Visual: the Add payment page's two baselines generated in the Playwright image against PostgreSQL, then the whole suite compared: 58 passed on PostgreSQL and 58 on sqlite; no other baseline changed. Shots: `docs/qa/12f-b/shots/c/`: the Add payment page at 1280 and 390, reviewed at both widths (the phone keeps the primary in view above the tab bar, as the phone rules say). Secret
scans clean; no secret-store read; no new file type; the bank method shown to staff carries nothing
beyond the last four (adversarial); a hostile amount, date, kind or note is refused with nothing
written and no job queued; a viewer cannot open or post the page; a card number in any field never
comes back; no provider name in the new files.

**TODO-VERIFY for the checkpoint.** The ACH debit from Add payment on live Stax: the sandbox has no
tokenization gateway, so card 1 was exercised against the fake provider only; the owner's steps once
Stax enables the gateway — on staging with a linked bank method: open a setup charge's Add payment,
choose the bank account, Debit on today, confirm on the Charge now page; the charge shows Pending;
the settle webhook marks it Paid within five business days; then the same with a later date and
watch the schedule fire.

**Verdict (C):** **PASS.** Every gate clean on both engines after the re-pins; two MINOR resolved, two MINOR deferred by written decision (C2-3 to Step 13, C2-4 to Batch D's pull request); the departures and the TODO-VERIFY listed for the checkpoint; no real ACH debit was made (the sandbox has no tokenization gateway).



## Batch D — Create invoice: Save, the e-mail prompt, Schedule settlement, the plan actions (branch `step-12f-b/batch-d`, finding 6)

**Scope.** Opus, three briefs and two reworks, rebased onto Batch C. D-1: How to collect reads Save
· Charge now · Schedule for a date · Record payment now · Set up autopay · Schedule settlement (Send
by e-mail is no longer a tile); Save keeps the draft and opens "Invoice saved. Send it to {operator}
by e-mail now?" over the store's Invoices tab (`ledger:invoices:saved`, a read; Send e-mail posts to
the existing send, due in 15 days unless set; Not now leaves the draft under Drafts with its own
Send by e-mail); the bank-debit tiles greyed with one reason, never hidden (Charge now: an admin,
the processor's debit flag, a bank account on file; Schedule, Set up autopay and Schedule
settlement: a bank account on file and the lease rule); a posted greyed choice refused; the native
radios unseen behind the card style; dates in the builder read "Nov 1, 2026" live and server-side;
the help page follows. D-2: Schedule settlement — the invoice's one line split into 2–12 equal bank
debits monthly, weekly or yearly from a first date through the installment engine (a manual charge
with no invoice, then the plan, in one transaction; the rounding difference on the last), the
preview sentence live (`ledger:invoices:settlement_preview`, JSON, read-only) and server-side,
"Settlement plan · k of N" from one label helper; `InstallmentCadence.yearly`,
`InstallmentPlan.origin`, migration 0054. D-3: Pause, Resume and Stop for installment plans on the
Autopay tab and the parent's card (Pause releases every unfired installment schedule in one
transaction and the plan is paused at once, a schedule that had already fired is recorded and shown;
Resume sets a schedule up for every unpaid installment and moves the passed ones to the business
days after today, one apart; Stop is the cancel behind its confirmation page; a paused plan keeps
its balance, is skipped by every debit path and lands in Needs review after thirty days; migration
0055). Fable: `tests/test_plan012fb_d_adversarial.py` (27 cases), the route digest (272) and the
stored choices re-pinned, one D-3 test rewritten for the new rule.

**Findings.** D1-4 (MAJOR, resolved): Schedule for a date was greyed by the pay-invoice flag, which
gates Charge now alone — staging runs with it off and would have lost scheduling; the schedule-based
tiles now look at the bank account and the lease only. D3-1 (MAJOR, resolved): a pause released only
the next schedule while an installment plan's schedules all exist up front, so later debits would
have run during a pause; every unfired schedule is released and recreated on resume. D1-1, D1-2,
D1-3, D1-5, D2-1, D2-2 (MINOR, resolved): the live date's format, the saved prompt's tab argument,
the autopay tile's bank reason, the help text, one label helper everywhere a plan is named, the
preview's own short refusals. C2-4 from Batch C (MINOR, resolved here): the Add payment primary
drops its figure until an amount is typed. Opus's own finding on D-2: the brief's conversion path
would have put a full invoice at the processor beside the schedules (double billing); the settlement
makes a manual charge with no invoice instead.

**Departures from the owner's message, for the checkpoint (plan D16, §9 items 5 and 6).** A
settlement takes an invoice with one line (the message speaks of "the invoice total"; two or more
lines are refused before any write). `InstallmentPlan.origin` is the one small column behind the
"Settlement plan" label. Pause / Resume / Stop apply to every installment plan, not only
settlements, with one rule for the table. The first settlement debit may be today for billing: it is
a schedule that fires tonight, not the pay call. Split keeps its three cadences on its own form; the
engine accepts yearly everywhere.

**Gate.** ruff, `ruff format --check` (511 files), mypy strict (511 files), `uv lock --check`, `makemigrations --check` after 0054 and 0055: clean; the pattern scan's only hits are the two route digests. sqlite: full suite 7,614 passed, 691 skipped, 106 deselected in 1,571 s; eight failures: six pins moved by design and re-pinned by Fable (the schedule tests of the allocation file now give their operator a bank account on file and expect the greyed tile's refusal before any write; the summary's send-button copy pair is gone), their files re-run 240 passed; and the operator page's query budget (52 for 51, 63 for 62), finding D3-2 below. PostgreSQL 16: migration selection 35 passed and 1 failed in 1,504 s (the round trip over an existing installment row: the test's raw insert predated the plan's `origin` column; the insert names it now, re-run on the container 1 passed), unit selection 8,269 passed, 3 skipped, 109 deselected with four PostgreSQL-only failures, all test-side and re-pinned by Fable (4edbc85: the pause race test still expected one release — it is one per installment now; the thirty-day review test updated an audit row, which PostgreSQL's append-only trigger refuses, so it turns the clock back for the pause instead), the six sqlite re-pins and the three new ones re-run on the container (257 and 4 passed); e2e 35 passed on each of four runs. Visual: every baseline regenerated in the Playwright image against PostgreSQL and the suite compared on PostgreSQL (58 passed) and on sqlite (58 passed); fourteen baselines changed: invoice-builder (the tiles' card style and Save), add-payment (the label without a figure), operator and operator-form (the hidden radios and the one label helper), store-charges, store-invoices (Send by e-mail) and store-schedules (the plan actions and the Status column) at both widths. Shots: `docs/qa/12f-b/shots/d/`: the fourteen changed baselines (1280 and 390), reviewed at both widths. Secret
scans clean; no secret-store read; no new file type; every new GET writes nothing (the prompt, the
preview, the Stop page); a viewer can neither open the prompt, post a greyed choice, pause, resume
nor stop; a pause never queues a pay call; hostile counts, dates, cadences and amounts are refused
with nothing written; no provider name in the new files.

**Verdict (D):** **PASS.** Every gate clean on both engines after the re-pins; two MAJOR and seven MINOR resolved, one MINOR closed by written decision (D3-2, a re-pin for a new review rule); the departures listed for the checkpoint; residual: the settlement debits settling across months and the pause's provider releases were exercised against the fake provider only (TODO-VERIFY for the checkpoint).

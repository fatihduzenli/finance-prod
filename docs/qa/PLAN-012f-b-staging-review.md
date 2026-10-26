# PLAN-012f-b — the owner's staging review: seven findings, five batches and a CI batch

Fable's Phase A plan for the owner's prompt of 2026-10-25 ("Step 12f-b — owner's staging review:
findings and additions"). Where the owner's message and `docs/design/DESIGN-SPEC.md` §4 / §4a
differ, the message wins; where this plan departs from the message, §3 names the departure and §9
asks. Staging runs 12f + 12f-a (`cccc008`). The QA record is `docs/qa/12f-b/QA-012f-b.md`.

## 1. Objective

Seven things the owner saw on staging, built as five batches plus a CI batch, each its own pull
request: the dashboard's "+ New invoice" creates an invoice through a store picker and the
dashboard gains a Cashflow card, a Payment status card and a mini calendar (A); stores and
operators are created and edited in modals, operators get a Company / Person type, the store form
gets an optional type, a state select, an optional landlord rent and a USPS address check (B); Add
payment becomes one page that either debits the operator's bank account through Stax or records a
payment received outside Stax (C); the invoice builder's How to collect becomes Save · Charge now ·
Schedule for a date · Record payment now · Set up autopay · Schedule settlement, with an e-mail
prompt after Save and equal debits through the installment engine (D); and Profit and loss, Daily
charges, Help and the shared States reach their spec sections (E). Batch 0 first: the guard lists,
the migration job's temporary limit, and the local STATUS commit.

## 2. Packages and scope

One Django project, `backend/` (apps `finance`, `ledger`, `payments`, `accounts`, `mail`,
`documents_storage`). Touched: `ledger` (models, services, views, forms, templates, static,
migrations), `finance` (templatetags, templates, static, `env_guard`, settings, a new `usps.py` and
`us_states.py`), `payments` only where Batch C reuses the existing charge-now and schedule jobs
(no new provider path), `.github/workflows/ci.yml` (the migration job's limit, Batch 0), tests,
docs. Out of scope: the provider adapter, Bicep and infra (the USPS secret names are documented
for the owner, the Bicep `secretRef` lines are a later infra slice the owner authorises), version
bumps, Step 13's migration-job split or squash (not squashed here), the P&L State filter and
grouping the spec draws (a behaviour change the owner excluded from E; §9).

## 3. Decisions (Fable's, under the message)

- **D1. Pull request per batch.** Branch `step-12f-b/<batch>` from `main`. Each batch ends with
  two publish actions, each its own Publish Request and the owner's written yes: (1) push the
  branch and open the pull request (named together, since the owner asked for PRs); (2) merge it
  (CI then deploys staging). Batch 0's branch carries the local STATUS commit `871a846`.
- **D2. One dialog component.** `finance/templates/finance/_dialog.html` (a `<dialog>` in the
  spec's confirmation frame, 440 px; 640 px variant for forms; full-width sheet on phones) with
  `finance/static/finance/dialog.js` (open on load when the server says so, close on ×, Escape and
  backdrop; focus kept inside). Server-rendered, no fetch: every dialog is a page that also works
  with scripts off (a server-opened dialog carries the `open` attribute as well as `data-open`;
  the caller writes its buttons in a `.dialog-foot` inside the content, so a form holds its
  fields and its buttons). Used by the store picker (A1), the store and operator forms (B1), the saved
  prompt (D) and the States section (E).
- **D3. Charts as inline SVG from a template tag.** `finance/templatetags/charts.py`: `bars`
  (paired bars and a line) and `donut`, from numbers the services compute; no library (spec §4a).
- **D4. Cashflow reconciles with the P&L by construction.** The card reads `pnl.rows` for the
  range grouped by month: Income = rent collected + fees collected (settled payments by
  `settled_on`, refunds netted, refundable deposits never, withheld deposits in their month,
  ADR-0012 §7); Expenses = landlord rent + other monthly cost + fee costs of the stores with a
  cost row in effect that month; Net = Income − Expenses. A store with no cost row is excluded
  from Expenses and counted in "n stores without a cost entered" (never zero). The test asserts
  the card's three totals equal the P&L totals for the same range.
- **D5. Payment status from the receivables' rules.** Over the shared range: Overdue = open
  charges due before today (unpaid or partly paid), Paid = charges paid in full with a settlement
  in the range, Upcoming = charges due in the range on or after today (`not_due`); Pending,
  Partly paid and Waived are not in the donut and read "+ n pending" under it.
- **D6. Mini calendar from `calendar.items_in`** with the month's window and the user's store
  filter off; one query set, no second event query. The popover is server-rendered per day
  (hidden list, shown on hover or tap); no JSON endpoint.
- **D7. The store picker's type-ahead** is a new read-only JSON route
  `ledger:search_stores` (`/search/stores.json?q=`, GET, billing and above, at most ten
  rows of `{"number","name","operator","url"}` from `services/search.py`). Without scripts the
  dialog is a form that searches the stores list.
- **D8. Modals keep POST-redirect-GET.** `ledger:stores:create` renders the stores list with the
  dialog open; `ledger:stores:edit` renders the store page with it open; the operator routes the
  same over the operators list and the operator page. Success redirects to the page behind with
  the green flash (that is the "refresh in place"); invalid input is held (`_hold`) and the GET
  re-opens the dialog with the errors inline. Old URLs keep working because they are the only
  URLs. No fetch, no second form.
- **D9. Operator type.** `Operator.kind` (`company` / `person`, default `person`), `first_name`,
  `last_name`, `contact_person` (new columns); `company` (existing) holds the company name.
  `display_name` stays the stored, shown name and is derived on save (company name, or
  "First Last"), so the ~100 templates that read it need no change. The migration sets every
  existing operator to `person` and leaves the new name fields empty; the form requires them on
  the next edit (no guessing from `display_name`).
- **D10. Store type** is a `StoreType` table (name, sort order, active; the migration seeds the
  five) edited in the Django admin on the `AuditedAdmin` pattern (the app's "System settings" link
  already goes there); `Store.store_type` is a nullable foreign key. The grey pill is one partial
  (`stores/_type_pill.html`) placed after the title in the store header, the Stores list, the
  Monthly rent overview's store column and the operator's store cards; nothing when empty.
- **D11. State select.** `finance/us_states.py` holds the 50 states and DC as (code, name). The
  `Store.state` column stays (50 chars); the form is a select of codes with a blank choice; the
  Stores list and grid filters and every screen show the full name through a `state_name` filter.
  Migration 0051 maps existing values (case-insensitive full name or code) to codes and blanks the
  rest. A new attention kind `store_state_missing` ("State needs choosing") lists active and
  prospective stores whose state is blank (Needs review). The operator's state stays free text
  (not asked).
- **D12. Landlord rent optional.** `StoreCreateForm.landlord_rent` becomes optional; empty
  creates no `StoreCost` row (cost unknown), `0.00` creates one. The P&L and the Cashflow card
  already read "no row" as unknown, never zero; the tests pin that the empty value lands there.
- **D13. USPS address check.** `finance/usps.py`: an httpx client with 5-second connect and read
  timeouts and no retry, OAuth client-credentials token cached in process, the Addresses API v3
  (`apis.usps.com`); `USPS_CLIENT_ID` and `USPS_CLIENT_SECRET` optional in `env_guard` (both or
  neither; one alone fails startup with a value-free message). Route
  `ledger:stores:check_address` (POST, JSON, billing and above) returns the standardised address
  or `{"verified": false}` with a plain reason; the form saves regardless and shows "Address not
  verified". The web app makes this one bounded outbound call itself (as it does for mail); the
  provider rule (worker only) is untouched. Egress: `apis.usps.com:443` recorded in
  `infra/README.md` and the checkpoint; the owner registers at the USPS developer portal and puts
  the two values in `finance-kv` under `usps-client-id` and `usps-client-secret`; the Bicep
  `secretRef` lines are a later, owner-authorised infra slice.
- **D14. Add payment, card 1 (ACH).** For a one-off charge: Debit on = today runs the charge's
  existing Charge now path (`one_off_charges.charge_now_confirm`, admin, the large-debit
  confirmation kept); a later date runs a new `one_off_charges.schedule_debit(charge, on, actor)`
  that sets the charge to `autopay_due` with that due date and queues the same `schedule_create`
  job the builder's Schedule for a date uses. For a rent charge, which has no Charge now path, card
  1 creates a re-debit one-off of the amount (the collections ladder's existing `redebit`
  category, `nsf.reschedule`'s creation generalised to `collections.redebit`) and schedules it.
  The bank select reads the snapshot (`bank_type · last 4 · nickname`; we store no more). **Departure:**
  no "Stax link to add one" is sent — operators never see a Stax page (owner's decision of
  2026-09-29, D1-B; `docs/DECISIONS.md` 250–253); the greyed card reads "No bank account on file —
  add one in the {processor} dashboard, then Update from {processor}" with the refresh action (§9).
  `PaymentMethod.CASH` is added (migration 0052 rebuilds the CHECK as 0048 did); Other stays for
  existing rows. The note is optional on card 1 and required on card 2.
- **D15. Builder: Save and the prompt.** The `draft` choice is labelled Save and the primary
  button reads Save; after the save the view redirects to `ledger:invoices:saved` (GET, the dialog
  frame over the store's Invoices tab): "Invoice saved. Send it to {operator} by e-mail now?" with
  Send e-mail (POST to the existing `ledger:invoices:send`, the Stax send unchanged, due in 15 days
  unless set) and Not now (back to the tab; the draft stays under Drafts with its Send action). The
  "Send by e-mail" tile goes; the `email` choice stays as the send's internal path. Charge now,
  Schedule for a date and Schedule settlement render greyed (`is-unavailable`, not hidden) with one
  line: "No bank account on file" / "Bank debits are off at {processor}" / "Charge now needs an
  admin"; the lease rule's line stays.
- **D16. Schedule settlement.** `CollectChoice.SETTLEMENT` converts the draft to a manual charge
  and splits it with the installment engine (`installments.create_plan`, collection `autopay_due`,
  count 2–12, first debit date, cadence); the remainder goes on the last debit (`split` already
  does). `InstallmentCadence.YEARLY` is added (migration; Monthly / Weekly / Yearly is the autopay
  control; `biweekly` stays for existing plans). **Departure, small:** `InstallmentPlan.origin`
  (`split` / `settlement`, default `split`) so the charge can read "Settlement plan · 3 of 10";
  without it the label would say "Installment plan" (§9). Pause / Resume / Stop on installment
  plans: Stop is the existing cancel relabelled (admin); Pause releases the next unfired debit's
  schedule and Resume recreates it (a `paused` status, the autopay plans' pattern); Reschedule
  stays. NSF rule and the live-Stax split path unchanged.
- **D17. Batch E is layout only** except the two rules the owner named (the P&L's unknown path
  for the new empty landlord rent; the Daily charges strip yellow when a lease was skipped, red
  when the last run or the last update failed). Not built in E: the P&L State filter and
  grouping, the Daily charges Confirm-per-row and the "Debit again / Set up autopay" row actions,
  Help's "Was this helpful?" (needs a backend), the Help search beyond a client-side filter of the
  topic list (§9).
- **D18. Visual suite.** Regenerate only the pages a batch changes, in the Playwright image
  against PostgreSQL, and list each file in the QA section; add pages the batches create
  (`store-modal`, `operator-modal`, `add-payment`, `invoice-saved`, `help`, `not-found`); the
  laptop and phone screenshots of every changed page go under `docs/qa/12f-b/shots/<batch>/`.

## 4. Interface contract

Routes (all under the existing namespaces; GET routes write nothing):
`ledger:search_stores` GET JSON; `ledger:stores:check_address` POST JSON;
`ledger:invoices:saved` GET; `ledger:invoices:settlement_preview` GET JSON (the preview sentence);
`ledger:plans:pause|resume|stop` POST (installment plans; `stop` keeps `cancel`'s view behind the
new name); the dashboard's range as `?range=this|3m|12m|custom&from=YYYY-MM&to=YYYY-MM`. Existing
routes keep their names and methods. Stored values added: `PaymentMethod.cash`,
`InstallmentCadence.yearly`, `InstallmentPlan.origin` (`split`, `settlement`),
`InstallmentPlan.status` `paused`, `Operator.kind` (`company`, `person`), `Store.state` as
two-letter codes. Models added: `StoreType`. Columns added: `Operator.kind`, `first_name`,
`last_name`, `contact_person`; `Store.store_type_id`; `InstallmentPlan.origin`. Env: `USPS_CLIENT_ID`,
`USPS_CLIENT_SECRET` (optional pair, names in `.env.example`, no values). Migrations: 0051 (store
type + state codes + attention), 0052 (cash + yearly + origin + paused), 0053 (operator kind and
names); each on the 0038 header pattern, listed in both guard lists (Batch 0's test enforces it).
Error shapes: service `Outcome(ok, message, field)` as today; JSON `{"ok": false, "message": …}`
value-free. Copy: plain language, the processor's name from `company_settings`, dates on screen as
"Nov 1, 2026", money through `money`.

## 5. Work breakdown (Opus slices; Fable's tests beside each)

**Batch 0 (Fable alone; tests and verification config):** both guard lists complete with 0046,
0048, 0050 and payments 0018; `test_every_migration_that_may_guard_a_column_is_in_both_guard_lists`;
`timeout-minutes: 45` on the migration job with the temporary comment; STATUS `871a846`.

**Batch A:** A-1 the dialog component and the store picker (D2, D7; the top-bar search gains
"New invoice for…" opening the same picker); A-2 Cashflow (D3, D4) and Payment status (D5) with the
shared range; A-3 the mini calendar (D6) and the dashboard grid (KPI · Cashflow · [Needs attention ⅔
| Payment status ⅓] · [Recent payments ⅔ | Mini calendar ⅓]; one column on phones).

**Batch B:** B-1 modals for the four forms (D2, D8); B-2 operator type (D9, migration 0053);
B-3 store type, state select, optional landlord rent (D10–D12, migration 0051); B-4 USPS (D13).

**Batch C:** C-1 `schedule_debit` and `collections.redebit`, `PaymentMethod.CASH` (D14, migration
0052 part); C-2 the Add payment page (two radio cards, Note, sticky Summary, the label that follows
the choice, the greyed card) on the three Add payment routes.

**Batch D:** D-1 the saved prompt and the Save tile; the greyed tiles (D15); D-2 Schedule
settlement (D16: the choice, the preview, `yearly`, `origin`, the labels); D-3 Pause / Resume /
Stop for installment plans on the Autopay tab.

**Batch E:** E-1 P&L page frame (KPI cards, two-row header, flags column; the empty landlord rent
in the unknown path); E-2 Daily charges frame and strip colours; E-3 Help frame and the topic
filter; E-4 States: the empty-state partial, flash styles, inline errors, the dialog frame's CSS,
404/403 in the shell.

Each slice is one Opus brief naming its files; two slices that edit the same file never run at
once. Fable writes `tests/test_plan012fb_<batch>_adversarial.py` per batch and extends
`tests/visual/test_pages.py`.

## 6. Test plan (written before the code)

Happy paths: the picker lands on `/stores/<pk>/invoices/new/` for "#1991", never on the list;
Cashflow totals equal `pnl.total` for the same months for each of the four ranges; Payment status
counts and amounts match the grid's statuses on a seeded month; the mini calendar's dots match
`items_in` for the month and the popover lists the day's items with the Calendar's kind classes;
New store through the modal creates the store and redirects to the list with the flash; an
invalid store re-opens the modal with the error under the field; an operator of kind company shows
its company name everywhere `display_name` is read; the store type pill shows only when set;
"Texas", "tx" and "TX" migrate to `TX`, "Tejas" to blank and a Needs review item; a store saved
with landlord rent empty has no cost row and the P&L shows "unknown" with the flag; the USPS check
with the fake transport returns the standardised address and "Use this" fills four fields; with no
credentials the form saves and shows "Address not verified"; Add payment card 1 today runs charge
now (job queued, charge Pending), a later date queues `schedule_create` with a single-occurrence
rule; a rent charge's card 1 creates one re-debit one-off; card 2 records Zelle / Wire / Check /
Cash and marks the Stax invoice when covered; Save then Send e-mail sends and the invoice is Sent
due in 15 days; Not now keeps the draft under Drafts; Schedule settlement of $10,000 in 10 monthly
debits makes 10 lines of $1,000.00, and $100 in 3 makes 33.33, 33.33, 33.34; the yearly cadence
dates fall on the same day each year; Pause releases the next schedule and Resume recreates it;
the P&L and Daily charges pages render in the shell with the new frames; the strip is yellow with a
skip and red after a failed run.

Boundaries and errors: ranges of one month and 36 months; a month with no payments and no costs;
a store with a cost row that starts mid-month; a `custom` range with `to` before `from` (one
message, nothing computed); a picker query of one character and of 200 characters; a picker query
with markup (escaped in the list); a modal POST without the CSRF token (403, nothing written);
an operator of kind person with a blank last name (refused inline); a state code not in the list
("ZZ", refused, nothing written); landlord rent `-1`, `0`, blank; a USPS 500, a timeout and a
malformed body (all "Address not verified", the save succeeds); card 1 with an amount above the
balance (refused), with a date in the past (refused), with no bank on file (card greyed and the
POST refused); card 2 with a reference holding a card number (refused by the validator); 1 and 13
payments (refused), a first debit date in the past (refused); Pause on a completed plan (refused).

Adversarial (Fable): every new GET writes nothing (the dialog GETs, the saved prompt, the JSON
routes); a viewer's POST to every new action is refused with nothing written; the JSON routes
answer 403 signed out; a forged `display_name` on the operator form is ignored (derived on save);
the USPS client never logs or echoes the credentials and sends them only to the token endpoint
(fake transport asserts the host); the migration of state codes is reversible and keeps the raw
text for unmatched rows in the audit; a settlement plan and an autopay plan on the same charge are
refused; a race of two Pauses leaves one released schedule; the picker never lists a closed store
without saying "closed"; no provider name in any new template or module (Rule 8); no ISO date on
any new screen.

## 7. Risk register

- Batch D's Pause / Resume on installment plans is the largest new behaviour (provider writes on
  release and recreate); it reuses `one_off_invoices.release_schedule` and `schedule_create` and
  is gated by the e2e and the FakeProvider; live settling across months is a TODO-VERIFY.
- Batch C's ACH from Add payment cannot be verified on the sandbox (ACH off, no tokenization
  gateway): tested against the fake only; TODO-VERIFY with steps for the owner.
- The USPS call is synchronous in the web request: bounded at 5 s, no retry, never blocks the
  save; credentials optional so staging without them is unaffected.
- Three migrations with CHECK rebuilds and a data migration (state codes): each reversible; the
  round-trip test grows; the job limit is raised to 45 minutes for this step only.
- Operator `display_name` derived on save: a stale `display_name` on an untouched person operator
  is kept as is (no guessing), so names do not change until staff edit.
- Shared code: the dialog component, the charts tag and the pill partial are read by several
  pages; each slice that touches them lists every consumer and Fable re-runs the visual suite's
  changed pages.
- Security-sensitive: the USPS secrets (names only in code, values in Key Vault), the JSON routes
  (role-checked, read-only), the modal POSTs (CSRF, roles as today), the bank select (snapshot
  fields only, never an account number).

## 8. Acceptance criteria

Per batch: Gate 1–4 clean on sqlite and PostgreSQL 16; the full suite green; the e2e green four
times on PostgreSQL; the visual suite green on both engines with only the listed baselines
regenerated; the QA section written with the laptop and phone screenshots of every changed page;
no new GET writes; no provider name outside `backend/payments/` (tests exempt); no ISO date on a
screen; `.env.example` names the two USPS variables with no values; every new migration in both
guard lists; the PR opened and merged only on the owner's written yes to each. For the step: the
checkpoint report after Batch E lists per batch what changed, the migrations (numbers), the tests
added, the baselines regenerated, the migration job's time on each PR, the open TODO-VERIFY items
(ACH from Add payment on live Stax; USPS with real credentials; settlement debits settling across
months) and every departure from the message with its reason; then the Publish Request.

## 9. Decisions needed from the owner (none blocks Batch 0 or A)

1. **Publish actions per batch (D1):** two authorisations per batch (push + open PR; merge), or
   one written yes covering both for a named batch.
2. **Card 1 without a bank account (D14):** no Stax invite exists and operators never see a Stax
   page (your 2026-09-29 decision); the card says to add the account in the processor's dashboard
   and offers the refresh. Confirm, or ask for an invite flow (a new step, not in this step).
3. **Rent charges on Add payment card 1 (D14):** a re-debit one-off scheduled for the date, the
   collections ladder's existing mechanism, since rent charges have no Charge now path. Confirm.
4. **Cash (D14):** added as a new method; Other kept for existing rows and still offered. Say so
   if Other should disappear from the form.
5. **`InstallmentPlan.origin` (D16):** one small column so the charge reads "Settlement plan";
   without it the label is "Installment plan". Confirm the column or the plain label.
6. **Pause / Resume on installment plans (D16):** built as asked (a paused status, release and
   recreate of the next debit's schedule); the cheaper alternative is Reschedule and Stop only.
7. **Operator kind for existing operators (D9):** every existing operator becomes a person with
   empty name fields that staff fill on the next edit; `display_name` is shown unchanged until then.
8. **Batch E exclusions (D17):** the P&L State filter and grouping, the Daily charges row actions
   and the Help "Was this helpful?" are not built; say which, if any, should be.
9. **USPS (D13):** register at the USPS developer portal (Addresses API v3, OAuth client
   credentials), store the two values in `finance-kv` as `usps-client-id` and `usps-client-secret`;
   the Bicep `secretRef` lines are an infra slice you authorise separately. Egress host
   `apis.usps.com:443` (and `apis-tem.usps.com` for the test environment).

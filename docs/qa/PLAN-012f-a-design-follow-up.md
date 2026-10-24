# PLAN-012f-a — Step 12f-a, the design follow-up (before Step 13)

Written by Fable on 2026-10-23 from the owner's Step 12f-a text, `docs/design/DESIGN-SPEC.md`
(§1 restored to the values built, §5 with the owner's date rule, §4 as the owner left it), the
eleven reference screenshots in `docs/design/`, and four read-only surveys of the code (the
builder and the schedules, the statement step, the dates on screen, the page families and the
tables). Batches as the owner set them, each gated, checkpoint before the push. Nothing is
pushed before the owner's "push". No new behaviour, URL or stored value beyond parts D and E.

## 1. Restated objective

Close the design step: confirm the five Batch A decisions as built and restore the spec's §1
(part A); accept "Open" and "Next", apply the §6 two-line phone card to every table and make every
on-screen date read "Nov 1, 2026" / "Oct 2026" while ISO stays in exports, the API, audit rows,
form values and URLs (part B); build seven more pages to their references and apply each family's
rules to its siblings, with new baselines and side-by-side comparisons (part C); make the lease-end
and transfer statement step a pure read until it is saved (part D); and add a sixth "How to
collect" choice, "Set up autopay", a repeating plan of bank debits with its own page on the
store's Autopay tab (part E, the only new behaviour besides D).

## 2. Affected packages

`backend/finance` (templatetags: `responsive.py`, a new `dates.py`; `static/finance/app.css`;
`templates/finance/` help pages), `backend/ledger` (templates of every family; services
`statements`, `lease_end`, `rent_run` (a pure preview), `invoice_builder`, `one_off_charges`, a
new `autopay_plans`; views `lease_ends`, `invoices`, `stores` (the Autopay tab), `calendar`,
`operators`; models and migrations for D (one column rename) and E (one model, two columns);
`forms.py`), `backend/payments` (jobs reused: `schedule_create`, `schedule_delete`; no adapter
change; the fake provider unchanged), `backend/tests` (visual world and suite, the families'
tests, Fable's adversarial files), `docs/` (DESIGN-SPEC §4 proposals, DECISIONS ADR-0014, this
plan, QA report, STATUS). Out of scope: the Stax adapter and mapping, the grid's layout, the
PWA shell, CI workflow, anything in `docs/design/*.png`.

## 3. Decisions

### 3.1 Part A — the Batch A points (done, documentation only)

All five were built in Step 12f Batch A as the owner now confirms: the nudged tokens
(`tokens.css` carries `--muted #5F6E85`, `--link #0B7C74`, `--paid-text #176B33`,
`--btn-primary-bg #0E8F86` with `--btn-primary-border #0B7C74`; `--faint` on no readable text),
"Settings" in the user card, the grid scrolling at 1,280, the login field "Username" without
"Forgot password?" or "Keep me signed in", "Monthly rent" in the sidebar and the bottom bar.
**One thing had slipped:** the owner's later copy of DESIGN-SPEC.md (committed with Batch C, when it
gained the operator and transfer sections) carried the original §1 values and no note. Restored
on 2026-10-23: the note at the top, the §1 values, the Buttons bullet and the Login decisions,
with the owner's sections kept. PLAN-011b now records the twelve-months rule as retired. Nothing
to build.

### 3.2 Part B — the checkpoint decisions

- **B1, B2.** "Open" on the attention rows and "Next" on the sign-in button stay. No change.
- **B3. The two-line phone card on every table (D1).** Today the rule is hand-written for the
  Charges table only (`charge-cell-*` classes, slice D-2). It becomes general in the one place
  every table passes through, `{% responsive_table %}` (`finance/templatetags/responsive.py`,
  which already parses each table to add `data-label` from the header row): the tag assigns a
  role to every body cell as `data-role`: `title` for the first cell, `amount` for the first
  `td.money` cell (or the cell whose header is Amount, Balance, Total or Price, in that order of
  preference), `status` for a cell holding a status badge, `actions` for a cell holding a
  button, link-button, form or input, `meta` for every other cell. The phone CSS (≤ 719.98 px)
  lays each row out as D-2 does: line 1 title (bold; its subline under it) with the amount bold at
  the right; line 2 the meta cells inline, each as "Label value" in muted small text separated by
  " · ", with the status badge at the right; the actions last, full width; the totals row one
  line; the empty row unchanged; the thead a zero-size box. Tables in `scroll` mode (the reports,
  the audit page) and tables with inputs in their rows (the builder's lines, the allocation lines,
  the deductions, the split, the setup checklist, enrol) keep their present phone layout: §6 says
  forms are one field per row. A table may override a role with `data-role` written in its
  template (the tag keeps an explicit one). The Charges table drops its `charge-cell-*` classes
  and the `partial` rule in favour of the general one; its Paid and Balance cells are meta cells
  that show only when non-zero and different from the amount (a `data-role="meta-if-partial"`
  override, the one special case kept, because the charge page carries them). The grid is not a
  responsive table and keeps its scroll.
- **B4. Dates on screen (D2).** One filter module, `finance/templatetags/dates.py`, loaded as
  `dates`: `|day` → `Nov 1, 2026`, `|month` → `Oct 2026`, `|moment` → `Nov 1, 2026 · 14:02`
  (New York, as the pages already assume), `|day_short` → `Nov 1` for the places the reference
  drops the year beside a month label (the calendar grid, the "Coming up" chips, the contact
  entry's date line); and `finance/dates.py` with `day_text`, `month_text`, `moment_text` for the
  services that build screen sentences (attention items, dashboard notes, the grid cell's
  "Pending · from <date>", the workspace header's texts, `store_context`, `workspace_cards`, the
  lease and store-cost refusal sentences, `closes_sentence` split into a screen and a CSV form,
  `pnl._label` split into a screen label and the URL part). Every `|date:"Y-m-d"`, `"Y-m"`,
  `"Y-m-d H:i"`, `"F Y"`, `"F"`, `"l, F j, Y"` on a page becomes the matching filter; `Y-m` as
  text ("Rent 2026-10") becomes "Rent Oct 2026" where the label is built for the screen
  (`receivables._rent_description`, `workspace._rent_description`, `dashboard._paid_for`,
  `reports._charge_description` on screen, `statements`, `lease_end`); the grid's month headers
  read "Jan 2026"; the P&L and grid range messages read "Jul 2026 to Oct 2026"; the month
  pickers' labels read "Oct 2026" with ISO values. **Stays ISO** (the owner's rule): CSV and
  spreadsheet cells (`csv_export.cell`), file names, the ICS feed, the JSON views, the audit
  rows' `before`/`after`, the audit page's JSON columns, `entity_id`s, Stax payloads, form field
  values, `data-*` attributes, `<time datetime>`, `?from=`/`?to=`/`?month=` and every URL. **Stays
  as stored:** `OneOffCharge.description` and notes written by earlier steps ("Re-debit of Rent
  2026-10", "Installment 1 of 3: Rent 2026-10", "Rescheduled: re-debit on 2026-11-05"), the
  builder's `RENT_ITEM` text ("Rent October 2026, store X", sent to Stax), `proration_text`
  ("15–31 Oct"), `statements.long_date` and `collections._long_date` (e-mail and PDF, already
  long form): these are stored values or provider text, out of this step's scope by the owner's
  last sentence; *for the owner*: whether new rows should be written in the new style is a
  separate decision (a data-migration-free change for new rows only). The rule and its exceptions
  go into DESIGN-SPEC §5 (done) and PLAN-012c §4.1's rules table (a "Dates" row, this step). A
  guard test (Fable's) renders the main pages for every role and fails on any ISO day or month
  outside attributes, `<time>`, inputs, scripts and the audit page's JSON cells; a template-source
  test refuses `|date:"Y-m-d"`/`"Y-m"`/`"F Y"` outside attribute values.

### 3.3 Part C — the seven references and their families (D3–D9)

The spec's §4 holds sections for the operator workspace, the transfer wizard and the login; the
four newest references (stores list, calendar, create invoice, charge detail) have **no section**
in the file the owner committed. PLAN-012f's rule stands: where the spec is silent the screenshot
wins. Fable writes a proposed section for each of the four into DESIGN-SPEC §4, marked "drafted
from the screenshot by Fable, for the owner's approval", so the build has a text to be checked
against. Facts only, as in 12e: a drawing's feature that does not exist is not built and is listed
here; nothing new is stored.

- **D3. Stores list (`stores-list-laptop.png`) — the list family:** header "Stores" with the
  counts line ("517 stores · 486 with a lease · 31 vacant": three cheap counts the list view
  already can give; *budget: +2 queries*), "Download spreadsheet" and "+ New store"; a filter card
  with the search box, State, Status, Balance (Any / Owing / Paid up / Overdue), Sort (Store
  number / Name / Balance) and Filter; the table: Store (number + name bold, city · state under
  it), Operator (link), Rent, Collected by (the lease's channel wording: Bank debit / E-mail /
  Manual), Balance as a badge ("$3,000.00 unpaid" unpaid family, "$2,100.00 overdue" overdue,
  "$658.06 partly paid" partial, "Paid up" paid, "Vacant" / "Vacant N days" neutral), Status
  badge (Active / Vacant / Closed), a chevron; the footer "1–7 of 7 · filtered from 517" with
  Previous · page · Next. The balance needs the per-store receivables the dashboard already
  computes (`receivables.open_balances`) — one query for the page's stores, within the list's
  budget. **Not built:** nothing in the drawing is outside the data; the Balance and Sort filters
  are new query parameters on an existing page (*for the owner*: allowed as "no new URL"? they
  are filters, not routes; Fable's reading: allowed). **Siblings:** Operators, Leases, Payments
  lists, Returned payments, Disputes, Unpaid balances, Search: the same header line with counts,
  the filter card's one row, the title-and-subline first cell, badges for money state, the
  chevron, the footer pager.
- **D4. Charge detail (`charge-detail-laptop.png`) — the detail family:** breadcrumb Stores › store
  › Charges › the charge; title with the status badge and a kind badge ("Deposit · held,
  refundable"); one muted line (store · operator · created <day> from <source> · due <day>);
  actions Add payment (primary), Split into installments, More ▾ (Edit, Waive, Cancel, Void: the
  existing actions, by role); three tiles Amount / Paid / Balance; cards Invoices (Number with
  subline, Sent, Due, Total, Status badge, "In <provider>: <status> · view"), Payments (empty
  state with the icon, "No payment yet. Add a payment · or wait for the invoice"), Activity (the
  audit rows of the charge: `audit._store_records` already lists them for the History tab; time ·
  action · by whom); right column DETAILS (category, collected by, due, cost, price, internal
  note), an "Open promise" tinted card when a promise names the charge (the contact log's promise
  with Kept · Reschedule · Broken, existing actions), MORE ▾ as a card listing the secondary
  actions. **Siblings:** one-off charge detail, invoice (sent) page, payment detail (tiles Amount
  / Refunded / Remaining; cards Refunds, Invoice, Charge, Activity), lease detail, calendar entry
  detail. **Not built:** nothing new; "Cancel in Stax" is the existing delete-invoice action
  worded by the copy rules.
- **D5. Create invoice (`create-invoice-laptop.png`) — the form/builder family:** breadcrumb;
  title with the muted context line (store · operator · lease from <day>) and the right-aligned
  "Draft · nothing reaches <provider> until you choose how to collect"; the Lines card with the
  item select (Rent · November 2026 / Existing charge · … / catalogue item / typed name) as the
  existing line kinds, description, qty, unit amount, total, trash icon; the dashed "+ Add line"
  and the help sentence; the How to collect card with the radios as bordered tiles two a row, the
  chosen one tinted, its fields inside the tile; right column SUMMARY (lines, total, memo, due,
  the primary button worded by the choice, Discard) and the help card. **Siblings:** New store,
  New lease (the setup checklist as a Lines-like table), Add payment with allocation (the
  allocation table in the Lines style, the SUMMARY card with the unallocated line), Edit operator
  and every `.form-card` page: title with context line, one card per group with a 15/600
  heading, the primary action right-aligned in a summary card on a laptop and sticky on a phone.
  **Not built:** nothing new; the sixth tile is part E.
- **D6. Calendar (`calendar-laptop.png`):** breadcrumb "Reports ›", title; the segmented Month ·
  List (the existing phone list mode becomes a laptop choice; **Week is not built**: no such view
  exists) and "Subscribe in Google Calendar" (the existing feed page); the month card with ‹ ›
  Today, Show (kinds) and Store filters; the grid with coloured chips per kind and the legend
  row; the right column: the selected or today's day panel (the day's entries with their amounts
  and links), NEXT 7 DAYS (the next week's entries grouped by day, amounts right), FEED (the ICS
  sentence with Copy link · Reset token: the feed page's existing actions). All from the entries
  the page already loads; the "next 7 days" needs the next week's rows (one more query, within
  budget). **Not built:** Week.
- **D7. Operator workspace (`operator-workspace-laptop.png`, spec §4):** the avatar with initials,
  name with Active and "Connected to <provider>" badges, the muted line (phone · e-mail · SMS
  consent · N stores), actions Statement · Edit · More ▾ (Connect, Archive; New invoice leaves the
  operator page as the spec says); the tab row with the Store select at the right; the strip of
  store cards; the Charges card with the Store column; right column CONTACT, PAYMENT (customer
  link, bank last four from the linked method's record, returns this year, open promise), RECENT
  CONTACT (the last two entries, Add entry), PORTAL ("Send invite" = the existing statement-link
  action; worded "Statement link" if the owner prefers — *for the owner*). **Not built:** nothing
  beyond the data.
- **D8. Transfer wizard frame (`transfer-wizard-laptop.png`, spec §4) for End lease and Transfer:**
  the header's right-aligned "Step N of M · nothing changes until you confirm"; the 260 px left
  card with the vertical step list, divider, "outgoing → incoming" (or the lease line) and the end
  date, the red "Cancel the transfer / lease end" link (the existing POST form as a text link with
  the confirm note); the main card with the step's 17/600 title and muted description; the
  statement step: three tiles (Open balance with "rent <day>, N of M days" from the proration,
  Deposit held with "collected <day>", Deductions with the line count), the Deductions table
  (reason select, note, amount, trash; the dashed "+ Add deduction" reveals the next of the eight
  rows), the SETTLEMENT box ending in the green "Refund due" or red "Still owed" row, the help
  line; the footer ← Back and "Save and continue →" right-aligned; on phones the step line and
  bar (built in 12f) above the card. The figures partial on the confirm pages follows.
- **D9. Login (`login-laptop.png`):** built in 12f Batch D; this step re-shoots it only.

### 3.4 Part D — the statement step writes nothing on GET (D10)

Today `lease_ends.statement`'s GET branch calls `lease_end.open_statement`, which creates the
missing months (catch-up), the end month's prorated charge and sets `statement_opened_at`; HEAD
writes too. The transfer wizard reuses that view. `statements.final` is already a pure read and
already shows a charge that does not exist as a preview line with the right totals; what a pure
GET loses is the catch-up of earlier missing months (rare: the daily run catches up active
leases). Decision:

- `open_statement` becomes `save_statement(end, lines, *, actor, today=None) -> Outcome`, called
  from the POST ("Save and continue") in place of `set_deductions` + the GET's write: under the
  lease and draft locks, with `blockers()` as today, it runs the catch-up, creates the end month's
  charges, writes the deductions, checks the offset, sets the marker and audits one
  `lease_end.statement_saved` row (with the charges) beside `lease_end.deductions_set`. GET and
  HEAD write nothing: no `RentCharge`, no `AuditLog`, no draft change.
- The preview: `statements._open_lines` already adds the end month's preview line ("Rent Oct
  2026: 1–19 Oct, 19 of 31 days of $2,000.00 = $1,225.81", no link); a new pure helper
  `rent_run.months_catch_up_would_create(lease, day)` returns the (period, seq, due, amount) tuples
  without writing, and `_open_lines` adds those as preview lines too, so the open balance, the
  offset and the settlement read the same before and after the save. The statement template marks
  preview lines "not created yet" (muted, no link); the tiles and the SETTLEMENT box use the same
  figures.
- The marker column `statement_opened_at` is renamed `statement_saved_at` (migration 0049, a
  rename only, reversible), and the places that read it (`in_progress`, `store_in_progress`, the
  store banner, `adopt_for_transfer`, `transfer.start`, the transfer start page) read the new
  name; the wording "in progress" now means "the statement step has been saved". PLAN-012b-b D9
  and D10 get a note. *For the owner*: keep the old column name instead (no migration) if a rename
  is unwelcome; Fable recommends the rename because the name would otherwise lie.
- Tests (Opus's and Fable's): a GET and a HEAD of the statement step by an admin write no row of
  any table (a snapshot of row counts before and after, and no `AuditLog`); the preview shows the
  prorated amount and the totals equal to the post-save ones; the save creates the charges once,
  is idempotent, respects the blockers, keeps the offset rule, and sets the marker; the transfer's
  `balance_blockers` list the charge only after the save; confirm's fallback still works when the
  step was never saved; `test_lease_end_drafts.py`, `test_plan012b_b_a_adversarial.py`,
  `test_transfer.py`, `test_pnl.py`'s `store_three`, `test_visual_world_adversarial.py` move to
  `save_statement`.
- The visual data set stops calling `open_statement` at build (the statement page now renders the
  preview with nothing written, so the baselines are engine-independent by construction); the
  P&L baseline returns to $6,000.00 for store 0202 and the dashboard, operator and grid baselines
  to the state before the world opened the statement; `test_visual_world_adversarial.py` asserts
  instead that a GET of every page in the suite writes no row.

### 3.5 Part E — "Set up autopay", a repeating plan (D11–D16)

The owner's words: a sixth "How to collect" choice; repeat the invoice as bank debits Monthly /
Weekly / Yearly from a first date, ending after N payments, on a date, or never; the plan shows on
the store's Autopay tab with Pause / Resume / Stop and an editable end rule; each run creates that
period's charges and the Stax schedule like a scheduled one-off; rent is excluded; ADR; tests for
the three cadences and the three end rules; the large-debit confirmation per debit.

What the code gives (survey): "Schedule for a date" creates one `autopay_due` one-off charge and
a `schedule_create` job at once, and the worker makes a single-occurrence Stax schedule
(`DTSTART…;FREQ=DAILY;COUNT=1`) up to 365 days ahead; a repeating Stax schedule cannot back one
charge (adoption requires `COUNT=1`); the catalogue's unique (lease, fee type) refuses a repeated
catalogue item; `CollectionSchedule` cannot hold weekly or yearly rules; weekly and yearly Stax
rules are unverified (TODO-VERIFY-019/036); no daily job creates one-off charges today; the
installment plan is the pattern (one charge per occurrence, each with its own single-occurrence
schedule, `plan` and `installment_no` on the charge).

- **D11. Model.** `AutopayPlan` (ledger migration 0049, with D's rename): `lease`, `operator`,
  `store` (denormalised for the tab), `item_name`, `description`, `quantity`, `unit_amount`,
  `amount` (the debit), `fee_type` (nullable, for reporting only), `cadence` (`monthly`, `weekly`,
  `yearly`: a new `AutopayCadence` TextChoices with its CHECK), `first_on`, `end_rule` (`count`,
  `date`, `never`), `end_count` (nullable), `end_on` (nullable), `state` (`active`, `paused`,
  `stopped`, `finished`), `next_on`, `made_count`, `large_debit_confirmed_at`, `created_by`,
  timestamps; `OneOffCharge.autopay_plan` (nullable FK) and `period_no`; a new
  `OneOffCategory.AUTOPAY_PLAN` ("Autopay plan") so the catalogue's unique constraint does not
  bite and the charge reads "Sign fee · 2 of 12" on the pages. **One line per plan**, the rule the
  Schedule choice already has (*for the owner*: the drawing says "repeat the invoice"; several
  lines would need a multi-line Stax schedule linked to several charges, which the schedule path
  cannot do today; Fable proposes one line now and a later decision for several).
- **D12. The builder.** `CollectChoice.AUTOPAY = "autopay"`, the sixth tile "Set up autopay —
  repeat this as bank debits" with its fields inside the tile: Repeat (Monthly / Weekly / Yearly),
  First debit (date, after today, within 365 days), Ends (After N payments / On a date / Never,
  with the N and the date fields shown by the choice), the large-debit tick when the amount is a
  large debit; the primary button reads "Set up $X monthly" (weekly / yearly). Offered under the
  Schedule choice's conditions (active lease, one item line, not rent, not an existing charge, an
  operator with a customer and a linked bank method); a rent line is refused with the existing
  sentence. `invoice_builder._autopay` creates the plan, audits `invoice_draft.sent` with
  `{"choice": "autopay", "plan": id}` and deletes the draft like Schedule does; then it creates
  the first occurrence at once (D13). Redirect: the store page's Autopay tab.
- **D13. Occurrences.** `autopay_plans.make_due(plan, today, actor)` creates the next period's
  charge when `next_on - LEAD_DAYS <= today` and the plan is active: one `OneOffCharge`
  (`category=autopay_plan`, `collection=autopay_due`, `due_date=next_on`, `price=amount`,
  `autopay_plan`, `period_no`, `creation_key=uuid5(plan.pk, period_no)`) through
  `one_off_charges.create_charge`, which enqueues `schedule_create` as today; then `next_on`
  advances by the cadence (monthly: same day, clamped to the month's last day as installments do;
  weekly: +7 days; yearly: same month and day, 29 Feb → 28 Feb), `made_count` increments, and the
  plan finishes when the end rule is met (count reached; next date past `end_on`). `LEAD_DAYS = 7`
  (*for the owner*: the Stax debit hour is 12:00 UTC on the due date and the daily tick runs at
  09:00 New York, so the occurrence must exist before its day; seven days gives the staff a week
  to see it on the Autopay tab). `collections_tick` calls `autopay_plans.run_due(today)` after the
  rent run; `/run/` lists the plan charges it created. The daily run is idempotent (the creation
  key; `make_due` skips when the occurrence exists). The large-debit rule: a plan whose amount is a
  large debit needs the tick at setup, stored as `large_debit_confirmed_at`, and every occurrence
  is audited `one_off_charge.large_debit_confirmed` naming the plan's confirmation (*for the
  owner*: the daily run has no human to ask per debit, so "per debit" is met by auditing each one
  against the setup confirmation; a plan above the provider limit is refused at setup).
- **D14. The Autopay tab.** A "Repeating debits" card above the schedules: one row per plan
  (item, amount, cadence words "Monthly from Nov 1, 2026 · 2 of 12 made · next Dec 1, 2026",
  state badge) with Pause / Resume / Stop (POST forms, billing for pause and resume, admin for
  stop, each with the single-submit guard) and "Change end" (a small form: the three end rules).
  Pause and Stop cancel the pending occurrence (the existing `cancel_charge`, which deletes its
  Stax schedule through `schedule_delete`) and set the state; Resume sets the state active and
  makes the next occurrence due again from today; Stop is final; Change end rewrites the rule and
  finishes the plan at once when it is already met. Each is audited (`autopay_plan.created`,
  `.paused`, `.resumed`, `.stopped`, `.end_changed`, `.finished`); the plan appears in the store's
  History tab (`audit._store_records`). The occurrence charges show in the tab's existing
  "autopay charges" table and on the Charges tab like any one-off.
- **D15. Elsewhere.** The lease-end and transfer blockers list a live plan ("a repeating debit is
  set up; stop it first") beside the one-off schedule blocker; the help page
  `autopay-and-daily-charges.html` gains a paragraph; `.env.example` unchanged (no new secret);
  Rule 8: no provider name in the new module or templates (`company.provider_name`); the Stax
  rule strings stay `FREQ=DAILY;COUNT=1` per occurrence, so TODO-VERIFY-019/036 are not widened.
- **D16. ADR-0014 — Repeating debits as a local plan of single-occurrence schedules:** context
  (the schedule path, adoption by `COUNT=1`, the catalogue constraint, unverified weekly and yearly
  rules at the provider), decision (local plan, one charge and one single-occurrence schedule per
  period, created seven days ahead by the daily run, rent excluded, one line), alternatives
  rejected (one repeating Stax schedule; a `CollectionSchedule` kind), consequences (the daily run
  gains a step; pausing cancels the pending debit; the plan is the source of truth for the end
  rule, Stax for each debit's state).

## 4. Interface contract

- `finance/templatetags/responsive.py`: `{% responsive_table %}` unchanged in its arguments;
  the body cells gain `data-role="title|amount|status|actions|meta|meta-if-partial"`, an
  explicit `data-role` in the template wins; `td.money` and `data-label` unchanged.
- `finance/templatetags/dates.py` (filters `day`, `day_short`, `month`, `moment`) and
  `finance/dates.py` (`day_text`, `day_short_text`, `month_text`, `moment_text`): `None` → "";
  `date`/`datetime` only (a `str` raises `TypeError` in tests, renders "" in templates).
- `ledger/services/lease_end.py`: `save_statement(end, lines, *, actor, today=None) -> Outcome`
  (replaces `open_statement` and `set_deductions` at the statement step; `set_deductions` stays
  for its other callers if any, else goes); `statement_saved_at` on `LeaseEnd`.
- `ledger/services/rent_run.py`: `months_catch_up_would_create(lease, day) -> list[Planned]`
  (pure; `Planned(period_month, seq, due_on, amount, proration_text)`); `catch_up_lease`
  unchanged and must create exactly those.
- `ledger/services/autopay_plans.py`: `create(draft_lines, lease, operator, *, cadence,
  first_on, end_rule, end_count, end_on, actor, confirmed, today) -> Outcome`, `make_due(plan,
  today, actor) -> OneOffCharge | None`, `run_due(today) -> list[OneOffCharge]`, `pause`,
  `resume`, `stop`, `change_end`; sentences as module constants.
- `invoice_builder.CollectChoice.AUTOPAY`; `forms.CollectForm` fields `autopay_cadence`,
  `autopay_first_on`, `autopay_end_rule`, `autopay_end_count`, `autopay_end_on`.
- URLs: the Autopay tab's four actions `stores:plan_pause|plan_resume|plan_stop|plan_end`
  (`/stores/<pk>/plans/<plan>/…`, POST; `RouteRule`s in `accounts/registry.py`): the only new
  URLs of the step, under E. No API, no JSON.
- Icons (`finance/templatetags/icons.py`): `chevron-right` (D3, the list rows) and `banknote` (D4,
  the empty Payments card); the shell test's icon list grows with them.
- Migrations: ledger `0049_statement_saved_and_autopay_plans` (the rename, the model, the two
  columns, the category value with its CHECK, `refuse_reverse` while plans exist). No payments
  migration (jobs reused).

## 5. Work breakdown (batches as the owner set them, each gated; checkpoint before the push)

| Batch | Scope | Opus sessions (each short, one file list) | Done when |
|---|---|---|---|
| A | A (docs, done by Fable), B3 the two-line card by roles, B4 dates, D the statement step | A1 D: services, view, migration, tests, world; A2 B4: the filters and the templates; A3 B4: the services' screen strings and the tests' expectations; A4 B3: the tag's roles, the CSS, the Charges table's classes removed, the tests | GET writes nothing (test); no ISO on any main page (guard); every responsive table reads as a two-line card at 390 in the e2e sample; the full gate green; baselines regenerated (expected to change broadly) |
| B | D3 list family, D4 detail family | B1 stores list + siblings; B2 charge detail + siblings | the two references read like the drawings at 1,440; the sibling pages carry the family's structure (markup tests); budgets kept |
| C | D5 builder and form family, D8 wizard frame | C1 builder + forms; C2 wizard frame + statement step look | the two references; the statement step's tiles and settlement from the pure figures |
| D | D7 operator workspace, D6 calendar, D9 login re-shot; the suite's new pages; the baselines; the seven side-by-side images | D1 operator; D2 calendar; D3 suite pages + report shots (Fable) | the three references; the suite covers every family page; all comparisons attached |
| E | D11–D16 autopay plans | E1 model + migration + service + daily run; E2 builder tile + form + redirect; E3 Autopay tab + actions + blockers + help; E4 ADR (Fable) | the three cadences and three end rules by test; the tab's actions; the large-debit audit per occurrence; the full gate |

Opus session rules as in 12f: every command under eight minutes, one test file at a time, the
sqlite file database or in-memory only, PostgreSQL only through Fable's gate.

## 6. Test plan (before implementation)

Happy paths: a GET and a HEAD of the statement step write no row; the save creates the end
month's charge once and the missing months; the preview totals equal the saved ones; every
`|date:` on the main pages renders the new forms; the month pickers keep ISO values with the new
labels; the CSV, ICS, JSON, audit JSON and query strings keep ISO (existing tests stay green,
unchanged); a plan with each cadence from a first date makes occurrences on the right dates
(monthly 31 → 30/28, weekly +7, yearly 29 Feb → 28 Feb); each end rule finishes the plan at the
right occurrence; pause cancels the pending occurrence and resume makes the next one; stop is
final; change end finishes a met plan at once; the daily run is idempotent across two ticks; the
two-line card at 390 for a list, a detail table and the charges table (e2e); the stores list's
counts line and the balance badges; the calendar's day panel and next seven days.

Boundaries: a plan whose first date is today or 366 days away is refused; an end count of 0 or 1;
an end date before the first date; a plan on an ended or paused lease; a rent line; a draft with
two lines; the operator without a customer or a linked method; the amount at the large-debit
threshold and above the provider limit; a statement save while a blocker appears between the GET
and the POST; the preview for a lease whose earlier months are missing; a date of None in every
filter; a table with no money column, with two money columns, with a badge in the first cell,
with inputs; a 29 Feb first date.

Error paths: the worker's `schedule_create` failing for an occurrence (the charge flags
`needs_review`, the plan continues); cancel of an occurrence whose schedule has made an invoice;
`save_statement` refused by the offset rule writes nothing (one transaction).

Adversarial (Fable's files): a viewer cannot pause, resume, stop a plan or change its end, and
billing cannot stop one or change its end (D14: billing pauses and resumes, admin stops and
changes the end; 403/redirect, no row); a plan's forms refuse a plan of another store in the URL; a hostile
cadence or end rule value is refused, not echoed; two concurrent ticks make one occurrence
(`select_for_update`, the creation key); a GET of every page in the visual suite and of the
statement step writes nothing; markup in an item name is escaped on the Autopay tab, the Charges
tab, `/run/` and the History tab; no ISO date on any main page for any role (the guard); no
provider name in any new file (Rule 8); a `data-role` written in a template wins over the tag's
guess; the two-line card never hides a cell that exists on the laptop except the Paid/Balance
pair of a non-partial charge row (rendered, not displayed, is counted as hidden: the test checks
`display`).

## 7. Risk register

- **Breadth of B4:** about 150 template sites, a dozen services, some 50 test files; the guard
  test is the safety net; stored strings and exports pinned by their existing tests.
- **B3 role guessing:** a wrong role hides nothing but can misplace a cell; the tag's rules are
  simple and overridable; the e2e sample covers one table per family.
- **D semantics:** "in progress" now begins at the save; the store banner and the transfer
  refusals move with it; the catch-up preview must match `catch_up_lease` exactly (one test
  compares them on a lease with gaps).
- **E and Stax:** one single-occurrence schedule per period, created seven days ahead; no
  repeating Stax rule, so nothing unverified is sent; a failing worker leaves the charge under
  review, not the plan; pausing deletes the pending schedule (the only Stax write), stop too.
- **E and money:** rent excluded by the existing rule; the large-debit confirmation stored and
  audited per occurrence; the provider limit refused at setup.
- **Migrations:** one ledger migration with a rename and a new model; `refuse_reverse` while
  plans exist; the migration round trips on CI are at 19:54 — the test suite adds one round trip
  of modest size; Step 13's split or squash decision stands.
- **Visual baselines:** change broadly in Batch A (dates, cards) and again per batch; each batch
  regenerates in the image against PostgreSQL and checks sqlite pixel-identical (the 12f lesson);
  the suite grows to the family pages.
- **The spec's missing sections:** Fable's proposed sections are the build's text until the
  owner edits them; the screenshot wins over both.

## 8. Acceptance criteria

1. A GET or HEAD of a lease-end or transfer statement step changes no row; the save does, once.
2. No ISO date renders on any main page for any role; every export, feed, JSON view, audit row,
   form value and URL keeps ISO; the stored strings named in 3.2 are unchanged.
3. At 390 px every responsive table in cards mode renders each row as title + amount, meta +
   badge, actions; nothing that exists on the laptop is hidden except the non-partial charge row's
   Paid and Balance.
4. At 1,440 the seven references read like the drawings in the side-by-side images; each sibling
   page carries its family's structure (markup tests name the elements).
5. A plan of each cadence and each end rule makes its occurrences on the dates the tests name,
   through the builder's sixth choice; Pause, Resume, Stop and Change end behave as 3.5 says; the
   daily run is idempotent; every occurrence that is a large debit carries its audit row.
6. Gate green on both engines per batch; baselines regenerated against PostgreSQL and
   pixel-identical on sqlite; secret scans clean; no new dependency; ADR-0014 written; the plan's
   decisions for the owner answered or deferred in writing.

## 9. Decisions needed from the owner (none blocks Batch A) — all nine decided 2026-10-24

1. **B4, stored text:** new one-off descriptions and notes ("Re-debit of Rent 2026-10",
   "Installment 1 of 3: Rent 2026-10", "Rescheduled: re-debit on 2026-11-05") keep ISO under the
   "no stored values" rule; say so if new rows should take the new style.
2. **D, the column:** rename `statement_opened_at` → `statement_saved_at` (Fable's
   recommendation) or keep the name.
3. **D3, filters:** the drawing's Balance and Sort filters are new query parameters on the
   existing list page; Fable reads "no new URL" as allowing them.
4. **D6:** the calendar's Week view is not built (no such view exists); Month · List only.
5. **D7:** the PORTAL card's "Send invite" is the existing statement-link action; its wording.
6. **E, lines:** one line per plan (the Schedule choice's rule) now; several lines later.
7. **E, timing:** the occurrence is created seven days before its due date by the daily run (and
   at once at setup for the first); another lead is a one-constant change.
8. **E, large debits:** confirmed once at setup, audited on every occurrence; the daily run
   cannot ask per debit.
9. **DESIGN-SPEC §4:** Fable drafts the four missing sections from the screenshots, marked as
   drafts; the owner replaces or approves them.

**Owner's decisions (2026-10-24), all nine as assumed:** 1 old notes keep the stored ISO text, only
the screen formats dates; 2 rename `statement_opened_at` → `statement_saved_at` (slice F below); 3
the list filters are new query parameters; 4 no Week view in this step, the calendar control is
Month / List (DESIGN-SPEC §4 Calendar updated); 5 the PORTAL card wording stands; 6 one line per
plan; 7 the seven-day lead; 8 the large-debit confirmation once at setup, audited per occurrence
(ADR-0014 Accepted); 9 the four drafted spec sections accepted. The owner's updated
`docs/design/DESIGN-SPEC.md` (adding §4 Profit and loss, Daily charges, Help, States, Add payment
and a §4a, with five new reference screenshots) landed the same day and was merged: the owner's text
wins where the two overlap (it replaces the four drafts); the §1 Batch A note and values, the §5
date rule and the Login decision were carried back in because the owner's copy predates them.

## 10. Slice F — confirm needs a saved statement (owner, 2026-10-24; after the checkpoint push, before Step 12f-b)

**Objective.** The lease-end and transfer wizards refuse Confirm unless the statement step was saved
in this wizard (the draft's `statement_saved_at`, set by the statement step's save). A direct POST
to either confirm URL on a draft whose statement step was never saved writes nothing — no charge, no
balance change, no lease or draft status change — and returns to the statement step with a message.
Batch A's decision (a), confirm's catch-up fallback, is withdrawn: confirm no longer creates the
statement's charges itself; it checks they exist (the save created them) and refuses otherwise.
Under the same slice, decision 2: the column `statement_opened_at` is renamed `statement_saved_at`
(model, services, a rename migration on the 0038/0040 pattern that reverses cleanly, DATA_OWNERSHIP
row, the audit payload key).

**Affected.** `backend/ledger/models.py` (the field), a new migration
`0050_rename_statement_saved_at`, `backend/ledger/services/lease_end.py` (`_confirm`:
`STATEMENT_REQUIRED` refusal with `field="statement"` replacing the `_create_statement_charges`
call; `save_statement` sets `statement_saved_at`; the module docstring),
`backend/ledger/services/transfer.py` (its confirm refuses the same way through
`lease_end.confirm_within`), `backend/ledger/views/lease_ends.py` and `views/transfers.py` (a
refusal on the statement field redirects to the wizard's statement step with the message; both GET
and POST of the confirm page redirect there while the statement is unsaved),
`docs/DATA_OWNERSHIP.md`. Out of scope: the statement step's own behaviour, the wizard frame, any
other column.

**Interface.** `lease_end.STATEMENT_REQUIRED = "Save the statement step before confirming."` (value-
free, final wording Fable's); `LeaseEnd.statement_saved_at: DateTimeField(null=True)`; `confirm` and
`transfer.confirm` keep their signatures and `Outcome` shape; the redirect targets are the existing
`ledger:lease_ends:statement` and `ledger:transfers:statement` routes. No new route, no new stored
value.

**Test plan (Fable writes `tests/test_plan012fa_f_adversarial.py`; Opus extends
`test_lease_end_drafts.py` and `test_transfer.py`).** Happy path: save then confirm on both wizards
still ends the lease with the same figures as before. Refusals, both wizards: a direct POST to the
confirm URL with a valid form and no saved statement answers a redirect to the statement step with
the message, and the database is unchanged (rent charge count, one-off count, every open balance,
the lease's `ended_on`, the draft's status and `updated_at`); the service call `confirm(end, actor,
confirmed=True)` returns `ok=False` with `field="statement"` and writes nothing; a GET of the
confirm page while unsaved redirects likewise; a transfer whose lease end was never saved refuses
before any incoming lease is created; a saved statement followed by a new charge still confirms (the
save's marker, not a figure match, is the rule). Adversarial: a forged `statement_saved_at` cannot
be set from any form field; the race of a save and a confirm in two transactions on PostgreSQL ends
with at most one confirmed row; the fallback test in `test_lease_end_drafts.py` (an existing charge
is left for confirm and the fallback) is re-pinned to the refusal. Migration: the round trip on
sqlite and PostgreSQL, a row's timestamp kept across the rename.

**Acceptance.** Both confirm URLs refuse an unsaved statement with nothing written on sqlite and
PostgreSQL; the tests above pass on both engines; `statement_opened_at` appears nowhere in
`backend/` outside migrations 0040 and 0050 and the historical tests that name the migration; the
full gate is clean; the QA report's Slice F section is PASS.

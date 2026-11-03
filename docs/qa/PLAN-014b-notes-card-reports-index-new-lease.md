# PLAN-014b — the NOTES card, the Reports index, the New lease page (owner, 2026-11-03)

Fable's plan under the root `CLAUDE.md` §1.2 for the owner's three page changes ("one small step
with the usual gate; push, PR and merge each on green and verify staging"). Opus builds; Fable gates
each item; the three go as three PRs merged in order. References named by the owner:
`docs/design/new-lease-dark-laptop.png` and `docs/design/reports-dark-laptop.png` — **not in the
repository at planning time**; the owner was asked for them. Until they arrive the layouts follow the
owner's text and the app's existing card and dialog patterns, and Fable compares the shots to the
references when they exist.

## 1. Objective

Three pages change. The store page gets a NOTES card under the LEASE card that always shows: an
"Add a note…" field, the pinned notes first then the latest (three in all, each cut to three lines
with "more"), and "All notes (n) ›". The Reports index becomes three groups of cards, each with an
icon, a one-line description, one live figure with a sub-line that reconciles with the report
itself, and "Open ›" on a wholly clickable card, with "Every report downloads as a spreadsheet." in
the header. The New lease page (from a vacant store's "+ Add lease") becomes five numbered cards
(Operator with a searchable picker and "+ New operator", Terms with a live proration line,
Collection with three payment-channel cards, Billing details once an operator is chosen, Move-in
charges) with a sticky Summary and, after saving, the dialog "Lease created. Send the move-in invoice
to {operator} now?"; the Status field and "New rent takes effect on" leave this form.

## 2. Order and branches

| # | Item | Branch | From | Why this order |
|---|---|---|---|---|
| 3 | NOTES card | `step-14/notes-card` | `main` (ecf0793) | smallest; one card, one service function |
| 2 | Reports index | `step-14/reports-index` | the notes-card head | independent of the lease form |
| 1 | New lease page | `step-14/new-lease` | the reports-index head | the largest; the one most likely to need rework |

Stacked so no two tasks edit `app.css` or a pinned test at the same time (§1.2): Opus builds the
next item on the previous head while Fable gates the previous one. Merges go in order; each branch
is retargeted to `main` when the one below has merged.

## 3. Affected packages

One package, `backend/` (Django). Apps: `ledger` (views, services, forms, templates, static, one
migration for item 1), `accounts/registry.py` (route rules), `finance` (`app.css`, `tokens.css`
pairs), tests, the visual suite's page list. Out of scope: `payments/` (no processor call changes),
`portal/`, `banking/`, `infra/`, CI, the Edit lease form (keeps Status and "New rent takes effect
on"), the Django admin.

## 4. Interface contract

### Item 3 — NOTES card (`ledger/services/notes.py`, `stores/_notes_card.html`)

- `notes.card_for(store) -> NotesCard` with `notes: list[Note]` (pinned first, newest pinned first,
  then the newest unpinned; at most 3, `CARD_SHOWN = 3`; soft-deleted excluded) and `total: int`
  (every live note of the store). Two queries at most, independent of the data.
- The card renders always, directly under the LEASE card (`stores/_header.html`), id `notes-card`:
  a one-line form `#note-card-form` ("Add a note…", input `body`, button "Save") posting to the
  existing `ledger:notes:add` (billing and above; viewers see no form), then the notes
  (`.card-note`, pin icon when pinned, body in `.card-note-body` clamped to three lines by CSS with
  a "more" link to `<notes tab>#note-<pk>` shown only when clamped — a CSS-only `-webkit-line-clamp`
  with the link always rendered for a body longer than `MORE_AFTER = 160` characters is acceptable),
  author's name (`|staff` as the existing card) and date), then `#notes-card-all` "All notes (n) ›"
  to the Notes & follow-ups tab. With no notes: the form and "No notes yet." only (no "All notes").
- `note_add` keeps its behaviour (redirect to the tab anchor; errors re-render the page) — a
  refused body (empty, over 2000) must show its error on the store page without a 500.
- No new route. No model change.

### Item 2 — Reports index (`ledger/views/reports.py`, `ledger/services/report_index.py`, `reports/index.html`)

- `report_index.figures(today) -> ReportFigures` computed inside the reports' `bounded()`:
  - `aging_total: Decimal`, `aging_stores: int` — `receivables.aging(today, AgingFilter())`: the
    report's grand total and its number of store rows (the same objects the Aging page shows).
  - `collected_month: Decimal` — `reports.payments_by_method(ReportRange(month, month)).total` for
    the current month (the same service and range the report page uses for that month).
  - `unmatched: int` — `len(reports.unmatched_items())` (what the Bank account check lists).
  - `billed_month: Decimal` — `reports.revenue_by_month(ReportRange(month, month)).total.total_billed`.
  - `margin_month: Decimal | None`, `margin_missing: str` — `pnl.total(pnl.rows(PnlQuery(month,
    month))).margin_billed`; when `None`, the sub-line names what is missing (`Figures.unknown`).
  - `calendar_week: int`, `calendar_next: str` — `calendar.items(start, end)` for today through the
    next 7 days (`next_days_window(today)`), the count and the first item's title and day.
- The page: `<h1>Reports</h1>` with the line "Every report downloads as a spreadsheet."; three
  `section.report-group` with `h2` "Collections", "Finance", "Operators & schedule"; each card
  `article.report-card` (an `<a>` wrapping the card, `id="report-<slug>"`) with `{% icon %}`, title,
  one-line description, `.report-figure` and `.report-sub`, and "Open ›". Aging's figure takes the
  unpaid tone (`--unpaid-text`) when above zero. The Customer statement card holds a small GET form
  to the operators list (`ledger:operators:list?q=`) with the field "Operator" and the button "Find"
  (no new endpoint). The Calendar card links to `ledger:calendar:page`. One column at 390 px.
- No new route. A query budget: the index page's query count must not grow with the data.

### Item 1 — New lease page (`ledger/views/leases.py`, `forms.py`, `leases/form.html`, migration 0070)

- `PaymentChannel.INVOICE = "invoice", "E-mail invoice"` (migration `ledger/0070_payment_channel_invoice.py`:
  the CHECK only; no data change; reversible while no lease holds `invoice`, refused otherwise with
  a sentence). The three cards: Autopay (bank debit) → `stax`; E-mail invoice → `invoice`; Paid
  outside Stax → `other`. The Edit lease form keeps its select, which gains "E-mail invoice".
  Every place that lists channels (`grep PaymentChannel`) is reviewed and named in the report.
- `LeaseCreateForm` on create: no `collection_status` (saved as `ACTIVE`); no `rent_effective_from`
  (already); `debit_day` a select 1–28 defaulting to 1; `second_debit_day` behind "Twice a month";
  `payment_channel` rendered as three radio cards; `prorate_first_month` a toggle; the deposit: the
  refundable setup fee type's price field (`setup_<code>_price`) rendered in the Terms card as
  "Deposit", the other checklist rows in the Move-in charges card; "+ Add item" reveals the active
  fee types not in the setup list (one row each, blank price = not charged; all rows shown without
  JS). The operator: the existing select (`OperatorContactSelect`) with a client-side search input
  that filters its options (no endpoint), each option also carrying `data-stores` ("#no Name" of the
  operator's other current stores, one query) and `data-initials`; the chosen operator shows as
  `#operator-card`. "+ New operator" opens the existing operator dialog (`operators/_form_dialog.html`)
  client-side (`data-dialog-open`), whose form carries `next=<this page's path with ?store=>`;
  `operator_create` honours a same-site `next` (`safe_next`) and redirects to it with
  `&operator=<pk>`; the create form takes `?operator=` as the initial operator.
- Billing details (card 4) as today (`_billing_step.html`), shown once an operator is chosen; the
  bank options: Later / "Use #NNNN's account" (only the chosen operator's other leases that have an
  account — each radio carries `data-operator`, shown when it matches; the server still refuses a
  lease of another operator) / Enter a new account (the existing refusal line when bank entry is off).
- Proration line (`#prorate-line`): client-side, "Nov 15–30: 16 of 30 days = $1,066.67" from the
  start date and the rent, rounding half up to cents exactly as `proration.prorated`; hidden when
  the start is the 1st or the toggle is off. The Summary (`aside#lease-summary`, sticky): operator,
  start, rent, collected by, first month (prorated), move-in charges, **First invoice total** = first
  month + move-in charges; "Create lease" and Cancel. Phone: one column, the Summary above the button.
- The move-in invoice: on save, when the lease is `ACTIVE` with an operator, a draft invoice is
  created (`invoice_builder.create_draft`) with one rent line (`rent_lease`, `rent_month` = the
  start month, the prorated amount when prorating) and one `existing_charge` line per setup charge;
  the view then renders the store page with the dialog `#lease-created` "Lease created. Send the
  move-in invoice to {operator} now?" — "Send e-mail" posts to `ledger:invoices:send` for that draft
  (`?store=`), "Not now" closes (the draft stays under Drafts). The rent run for that month must then
  create no second rent charge (the rent line is the month's charge). If no draft can be made (no
  operator, a refused line) the lease is still saved and the page says why in one sentence.
- Header: "New lease for #<no> <Name>" with the store's status badge (Vacant).
- Routes: none added beyond none; `operators:create` gains the `next` parameter (same rule).

## 5. Work breakdown

| Task | Files | Done when |
|---|---|---|
| 3.1 `notes.card_for` | `ledger/services/notes.py` | pinned-first order, 3 max, total; two queries |
| 3.2 the card | `stores/_notes_card.html`, `_header.html`, `views/workspace_cards.py`, `app.css` | always shown; form for billing+; clamp + "more"; "All notes (n) ›"; empty state |
| 3.3 tests | `tests/test_notes*.py`, `test_store_workspace.py` (budget), visual baselines (gate) | every case in §6 |
| 2.1 `report_index.figures` | `ledger/services/report_index.py` | each figure equals the report's own |
| 2.2 the page | `views/reports.py`, `reports/index.html`, `app.css`, `tokens.css` pairs if any | three groups, cards, figures, form, header line, phone |
| 2.3 tests + visual page | `tests/test_report_index.py`, `tests/visual/test_pages.py` (`reports`) | reconciliation and budget tests green |
| 1.1 channel value | `ledger/models.py`, `migrations/0070_…`, `tests/test_migrations_roundtrip.py` guard lists | CHECK accepts `invoice`; reverse rule |
| 1.2 form and view | `forms.py`, `views/leases.py`, `views/operators.py` (`next`) | fields as §4; `?operator=` initial; `next` honoured safely |
| 1.3 templates and JS | `leases/form.html`, `_billing_step.html`, new `ledger/static/ledger/new_lease.js`, `app.css` | five cards, picker, operator card, proration line, summary, phone |
| 1.4 move-in invoice + dialog | `views/leases.py`, `services/one_off_charges.py` or a new `services/move_in.py`, `leases/_created_dialog.html` | draft with rent + setup lines; dialog; Send e-mail; rent run idempotent |
| 1.5 tests | `tests/test_new_lease_page.py`, `tests/e2e/test_new_lease.py`, re-pins named | every case in §6 |

## 6. Test plan (written before the build)

**Item 3.** Order (two pinned, four unpinned → the two pinned newest-first then the newest
unpinned); exactly 3 shown with `total` = 6; deleted notes excluded; empty store → form and "No
notes yet." and no "All notes"; a viewer sees no form; billing posts a note through the card and
lands on the tab anchor; an empty body and a 2001-character body are refused with the message on
the store page (no 500); a body of 200 characters renders the "more" link, one of 50 does not;
the store workspace query count does not grow with the number of notes (budget test); the card is
in the right column after the LEASE card on every tab (one assertion per tab); the Notes &
follow-ups tab is unchanged (its test file still green). Adversarial: a note body with `<script>`
is escaped in the card; a pinned note of another store never shows.

**Item 2.** For each figure a test that builds data and asserts the index figure equals the number
the report page itself shows for the same day/month (Aging total and store count against the Aging
page; collected this month against Payments by method for the month; unmatched against the Bank
account check; billed this month against Revenue by month; the margin against the P&L page; the
calendar count and next item against the Calendar page for the same window). The margin's "—" and
its sub-line when costs are unknown. Zero states ("$0.00", "0 stores", "no items"). The statement
form submits to the operators list with `q`. Every card is one link with `Open ›`. The header line.
A query budget independent of the data (stores × charges × payments). A viewer can open it (the
registry says read-only). The reports' statement timeout wraps the figures. Adversarial: a charge of
another store's ended lease still counts where the report counts it (the figure follows the report,
nothing else).

**Item 1.** The form has no Status and no "New rent takes effect on"; a saved lease is `ACTIVE`;
debit day defaults to 1 and refuses 29; the three channel cards map to `stax`, `invoice`, `other`
and the edit form offers "E-mail invoice"; 0070 forward and back on sqlite and PostgreSQL, the
reverse refused while a lease holds `invoice`; the deposit field is the refundable fee type's price
(a lease saved with a deposit of 500 has that setup charge; none when blank); "+ Add item" rows
charge only with a price; `?operator=` preselects; the operator options carry `data-stores`
(another current store listed, an ended one not) and `data-initials`; `operator_create` with
`next=/leases/new/?store=…` redirects there with `&operator=`, and refuses an off-site or
`//` next (falls back to the operator page); the bank radios carry `data-operator` and the server
refuses `same:<lease of another operator>`; the proration formula: an e2e test (Chromium) types
2026-11-15 and $2,000.00 and reads "Nov 15–30: 16 of 30 days = $1,066.67" equal to
`proration.prorated`'s amount, and the Summary's first invoice total = that + the move-in charges;
the move-in invoice: a draft with the rent line and the setup lines exists after save, "Send
e-mail" sends it (the existing send path), "Not now" leaves a draft; the rent run for the start
month then reports the rent charge already created (no duplicate); no operator → lease saved, no
draft, one sentence; the dialog text names the operator; the Vacant badge in the header;
breadcrumbs unchanged; every control labelled (`test_design_accessibility`); Rule 8 fence (no
"stax" literal in templates — the provider name through settings); FROZEN_ROUTES unchanged (no
new route) or re-pinned and named. Adversarial: a posted `payment_channel=zelle` on create is
refused (not one of the three cards) while the edit form accepts it; a `next` to `/portal/` is
refused; a creation key replay creates one lease and one draft.

## 7. Risk register

- **The move-in invoice and the rent run** (item 1): a rent line not recognised by the run would
  double-bill the first month. Mitigation: the explicit idempotency test; Opus must read
  `invoice_builder`'s rent-line path and `rent_run`'s "already created" rule and report them.
- **A new channel value** touches a CHECK constraint and every channel listing; a missed listing
  shows "invoice" raw. Mitigation: the `grep` list in the report; the reverse refusal.
- **`operator_create`'s `next`**: an open redirect if unvalidated. Mitigation: `safe_next`-style
  same-site check, tests for off-site and `//`.
- **Figures that drift from their reports** (item 2): the figures must call the report services
  with the report's own ranges; the reconciliation tests pin this.
- **Query growth** on the store page (item 3) and the index (item 2): budget tests.
- **Visual churn**: the store tabs' baselines all move (the NOTES card); the new-lease baseline
  moves; the reports index joins the suite. Fable reads them by eye in both themes and widths.
- **Shared files across items** (`app.css`, pinned tests): stacking avoids concurrent edits.
- **Missing references**: the layouts may need a second pass when the owner's two images arrive.
- Nothing is irreversible; no secret, no processor call, no external write.

## 8. Acceptance criteria

1. Each item's gate (ruff, format, mypy strict, lock, makemigrations on both databases, the secret
   scan, the full sqlite suite, the PostgreSQL migration and unit selections, e2e ×4, the visual
   suite regenerated on PostgreSQL and compared on both) is green, recorded in `QA-014.md`.
2. Every §6 case has a test that fails without the change.
3. Item 2's six figures equal the reports' own numbers in the reconciliation tests.
4. Item 1: a lease created through the page is `ACTIVE`, carries the chosen channel, has its setup
   charges and a move-in draft, and the next rent run creates no second rent charge for the start
   month.
5. Item 3: the card shows on every store page, three notes at most, pinned first, with the form for
   billing and above only.
6. No new route except none; no production file edited by Fable; findings resolved or deferred in
   writing; staging verified on each merge commit.

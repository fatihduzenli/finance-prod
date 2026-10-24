# QA Report — PLAN-012f-a: Step 12f-a, the design follow-up

Fable's gate per batch (root CLAUDE.md §1.3–1.5). Commands run from the repository root unless
noted; the PostgreSQL legs on a throwaway `postgres:16` container; the visual suite inside
`mcr.microsoft.com/playwright/python:v1.63.0-noble` against PostgreSQL, with a sqlite render
checked pixel-identical (the 12f lesson). Nothing is pushed before the owner's "push".

### Batch A — the follow-ups: part A (docs), B3 the two-line card by roles, B4 dates on screen, D the statement step as a pure read

**Scope.** *Part A (Fable, docs):* DESIGN-SPEC §1 restored to the values built with the Batch A
note and the Login decisions (the owner's later copy of the file had reverted them), §5 gained
the date rule, §4 gained Fable's four drafted sections (stores list, charge detail, create
invoice, calendar: marked drafts, the screenshot wins); PLAN-011b's twelve-months rule marked
retired; PLAN-012c §4.1 gained a "Dates" row. *A1, part D (Opus):* `lease_end.open_statement`
removed; `save_statement(end, lines, *, actor, today)` creates the catch-up months and the end
month's charges, writes the deductions with the offset rule, sets the marker and audits
`lease_end.statement_saved`, all in one transaction under the lease and draft locks with the
blockers re-read; `_create_statement_charges` shared with confirm, which now creates the same
charges before computing its figures (its missing-charge fallback refuses `CHANGED_MEANWHILE`
instead of creating); `rent_run.months_catch_up_would_create` (pure) and `catch_up_lease` share
one selection; `statements._open_lines` previews the planned months and the end month with the
same totals; `transfer.balance_blockers` counts a planned charge as owed like an existing one;
the view's GET and HEAD write nothing; `_figures.html` marks preview lines "not created yet";
the column keeps its name `statement_opened_at` pending the owner's answer (plan §9.2); the
visual data set no longer opens the statement at build. *A4, B3 (Opus):* `{% responsive_table %}`
assigns `data-role` to every body cell in cards mode (title by `cell-title` else the first cell,
the cells before it meta; amount by `money` else the Amount/Balance/Total/Price header; status by
the first badge; actions by button/form/link-button and then by column; meta otherwise,
`meta-badge` when badges only; summary tables all meta; form tables no roles; an explicit role
kept; the table gets `role-cards`), and app.css's phone block lays each row out as a wrapping
flex row (title + amount; meta line with the badge; actions full width; totals one line); the
Charges table drops the D-2 classes, its Paid and Balance cells carry `meta-if-partial`. *A2, B4
(Opus):* `finance/dates.py` (`day_text`, `day_short_text`, `day_long_text`, `month_text`,
`moment_text`, New York, fixed names) and the `dates` filter library; 63 templates swept (ISO
left only in attribute and machine values, listed in the report below); the print footer's
`{% now "M j, Y" %}`. *A3, B4 (Opus):* the services' screen strings and the tests' expectations
(see below). *Fable's tests:* `tests/test_plan012fa_a_adversarial.py` (4: every suite page's GET
and HEAD write no row; the statement's preview and marker; a viewer's and billing's save refused
with nothing written; a refused save creates no charge), `tests/test_plan012fa_b4_adversarial.py`
(2: no ISO as page text on any main page for any role, the stored strings allowed by pattern; no
ISO date filter on page text in any template), `tests/test_plan012f_d2_adversarial.py` and
`tests/e2e/test_phone_charge_cards_12f_d2.py` rewritten to the roles contract (roles in order on
both workspaces; the partial row; no duplicated text; the role rules only in the phone block;
the geometry of a charge row, a stores-list row and a recent-payments row at 390; every cell on
the row's line at 1,280); `tests/test_visual_world_adversarial.py` removed (superseded).

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| A-1 | MAJOR | lease_end confirm path | With a pure GET, a confirm without a prior save computed its figures from preview lines and created only the end month, leaving `statement_json` with lines that had no charge and the earlier months uncreated (a gap the old flow hid because the GET created them) | Resolved: confirm creates exactly what the save creates, before its figures (`_create_statement_charges`); tests: confirm without a save creates the months and leaves no line without a charge id; confirm after a save creates nothing |
| A-2 | MAJOR | transfer.balance_blockers | A transfer could confirm while planned (not yet created) months were owed: the blocker skipped lines without a charge ("it blocks once it exists"), which the old GET made true and the pure GET no longer does | Resolved: a planned charge blocks like an existing one, linked to the lease until it exists; tests for the sentences, the offset covering them, and the never-"not charged yet" wording |
| A-3 | MINOR | tests (fixtures) | Eight wizard tests relied on leases with months of unbilled history that only a GET used to catch up | Resolved as fixture changes (start dates; no expected figure changed) |
| A-4 | MINOR | the first A4 contract | My brief said grid areas; a grid area holds one cell and the meta line holds several | Accepted deviation: a wrapping flex row, documented in the tag and the CSS |
| A-5 | MINOR | tests/test_plan012fa_a_adversarial.py | Fable's row-count snapshot was taken before the sign-ins, so the session rows counted as writes | Resolved (snapshot after the sign-ins) |

**Decisions taken (Fable) and for the owner.** The dashboard's collection card reads "Oct 2026
collection" (the rule applied to a sentence; *for the owner*: "October collection" would need an
exception to the rule); the contact entry's date keeps its year (the rule drops it only where the
surrounding text names it); the calendar's add-tooltip reads the new form (a person reads it); the
PDF's "Last month" heading reads "Oct 2026" while `long_date` stays; statements confirmed before
this change may still hold a line without a charge id in their `statement_json` (historical rows,
unchanged). The open question of the previous visual run's pass (QA-012f addendum) is now moot:
nothing is written on GET, so the suite cannot depend on page order.

**Gate 1.** ruff, `ruff format --check` (466 files), mypy strict (466 files), `uv lock --check`,
`makemigrations --check` on sqlite and PostgreSQL 16 ("No changes detected"): all clean. **Gate 2.**
sqlite 6,625 passed, 664 skipped (19:51) with seven failures, every one a test expectation that the
batch itself changed (the promise sentence's wording on the page, with its CSV assertion split off
and kept ISO; the due cell's role test pinned ISO; three wizard fixtures whose leases had unbilled
months that the stricter transfer blockers now list, started in the end month as A1's fixtures
are) — fixed by Fable, the five files re-run green on both engines (sqlite per file; PostgreSQL 59
passed in one run against the gate's container); PostgreSQL "Migration tests" 34 passed (16:29),
"Unit tests" 7,252 passed, 3 skipped (20:58) with the same seven (collected before the fixes); e2e
29 passed in each of four runs on PostgreSQL (the 28 of 12f plus Fable's second phone-cards case).
Visual: the thirty baselines regenerated in the image against PostgreSQL (34 passed), compare runs
34 passed on PostgreSQL and on sqlite, a sqlite regeneration equal to the PostgreSQL baselines in
all thirty files; 29 of 30 baselines changed (dates and cards). **Gate 3.** Diff read: the view's
GET branch calls no service; `save_statement` and `_confirm` share `_create_statement_charges`
under both locks with the blockers re-read; `balance_blockers` treats a planned line as an existing
one; the tag decides roles per row then per table, keeps explicit roles, skips form tables and
scroll mode; the CSS hangs only on `role-cards` inside the phone block; every export path takes an
explicit ISO form (`csv_description`, `closes_csv_text`, `_charge_description(export=True)`,
`pnl._label` for the query, `csv_header`, `data-month`); the stored strings named in the plan are
unchanged. **Gate 4.** Secret scans (script and pattern, the diff and every new file) clean; no
provider name in a new file; no new input beyond none (no new URL); the preview lines carry no
link; a viewer's and billing's save refused with nothing written; a refused save creates nothing;
hostile `data-role` values are not read by the tag (an explicit role is copied as written only
into the attribute it came from, never into a class). Fable's cases: every suite page's GET and
HEAD write no row for billing and admin; no ISO date as text on 27 pages × 3 roles; no ISO date
filter on page text in any template; the roles' order on both workspaces; the partial row; the
role rules only in the phone block; the phone geometry of a charge row, a stores-list row and a
recent-payments row; every laptop cell on the row's line.

**Visual check.** The regenerated baselines read as intended: the dashboard's dates in the new
forms throughout; the statement step with both planned charges as "not created yet" preview lines
and the same totals; the operator page's phone cards with the store as a meta line; the charges
tab's two-line cards with the partial row's Paid · Balance line.

**Residual risk (A).** Statements confirmed before this change may hold a `statement_json` line
without a charge id (historical rows). The stored descriptions written before this step keep ISO
on screen ("Re-debit of Rent 2026-10"); the guard allows them by pattern (plan §9.1). The
`(debit 2)` and one-off branches of `parent_screen_label`, and the `None` branch of the calendar's
promise message, run without a direct test. The audit page's Time column lost its seconds and
zone word (the CSV keeps both).

**Verdict (A):** PASS (two MAJOR and three MINOR resolved; the owner's questions of plan §9 stand)

### Batch B — the list family (D3) and the detail family (D4)

**Scope.** *B1 (Opus):* `ledger/services/stores.py` gains `store_list(params, today)` (every store
with its current lease, operator, channel, rent and last lease end in one query of correlated
subqueries; the balances from the shared `receivables.open_balances` grouped by store; the filters
`q`, `state`, `status` (active / vacant / closed), `balance` (owing / paid / overdue) and `sort`
(number / name / balance) parsed against fixed lists, applied over every store before the sort and
the page; an unknown value falls back and is never echoed); `views/stores.py` and the operators
list's store count; `_list_footer.html` ("1–7 of 7 · filtered from 517" and the pager with the
current page as a dark pill); `stores/list.html` to the reference (counts line, one filter row,
the store title with "City, ST" under it, Operator, Rent, Collected by, the Balance badge in its
family, the Status badge, a chevron); the siblings Operators, Leases, Payments, Returned payments,
Disputes, Unpaid balances and Search with the counts line, the title-and-subline first cell, badges
for money state and status, the footer where the page is paged; the `page_numbers` filter and the
`chevron-right` icon; app.css's "List family" block. Budget: the stores list 11 → 14 (the counts,
the balances), pinned and constant. *B2 (Opus):* `ledger/services/detail_cards.py` (read helpers:
`activity` in one query over the record's audit rows, newest first, capped at 20 with the "more"
flag, notes masked; `invoice_rows` with the line count and the builder draft; `kind_words`,
`one_off_source`, `rent_source`, `header_line`, `lease_open_balance`) and `views/detail_cards.py`
(`activity_context`: the "All activity" link to the store's History tab or the audit page filtered
to the record, admin only); the partials `_detail_activity`, `_detail_invoices` (the provider status
as copied in a teal pill, "view" to the builder page for billing), `_detail_payments_empty`,
`_detail_promise` (the existing outcome links as text); the six pages rewritten on one frame
(breadcrumb with Charges or Payments, the title with the status and kind badges, the muted
header line, the primary and More ▾, the three tiles, the relation cards, Activity, the DETAILS,
Open promise and MORE cards): one-off charge, rent charge, payment (Amount / Refunded / Remaining),
sent invoice (Total / Paid / Balance), lease (Rent / Open balance / Deposit), calendar entry; the
`banknote` icon; app.css's "Detail family" block. Budgets: one-off 35 → 37, rent 28 → 28, payment
17 → 17, sent invoice 25 → 26, lease 24 → 30, calendar entry 10 → 11, each pinned and constant.
*Fable:* `tests/test_plan012fa_b_adversarial.py` (8: every list filter control fed markup, overlong,
traversal and odd values answers on site and echoes none of them; a viewer sees no primary action
or form link on any list; the stores counts line and footer equal the database; the balance badge's
family per store; markup escaped on every list and detail page; the detail page's actions by role;
the balance sort and filter; the vacant filter versus the counts line).

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| B-1 | MINOR | app.css (B1) | The list family's phone rules opened a second `@media (max-width: 719.98px)` block with a `data-role` rule, outside the one block the role rules are confined to | Resolved: folded into the existing phone block (one block in the file) |
| B-2 | MINOR | tests/test_plan012fa_b_adversarial.py (Fable) | "With a lease" counted through an outer join and so counted a store with no lease | Resolved (`leases__isnull=False`), found by Opus |
| B-3 | MINOR | the phone's list cards | The operator link's 44 px tap height stretches the meta line and splits the two badges over lines | Open: a CSS tidy of the list family's phone meta line (the role card's inline links at text height while the row stays ≥ 44 px), for the Batch D cleanup |
| B-4 | — | brief B1 | Fable asked for the balances "in one query grouped by store"; Opus kept the shared `open_balances` (four queries, six with a re-debit carry) so the list's figures cannot drift from the dashboard's | Accepted: the shared helper; the budget +3 as planned |
| B-5 | — | brief B2 | Fable's "≤ 25 and constant" budget was already untrue for four of the six pages before the slice; the lease page's Open balance tile adds the receivables read | Accepted: the pages pinned at their constants (one-off 37, rent 28, payment 17, invoice 26, lease 30, entry 11) |

| B-6 | MAJOR | tests/test_plan012fa_b_detail.py (Opus) | Twenty fixtures assigned `provider_status` directly: green on sqlite, refused by PostgreSQL's Rule 2 trigger, so the file could never pass CI | Resolved: the fixture goes through the sync layer; re-run green on both engines; no production line writes the column |

**Decisions taken (Fable) and for the owner.** The counts line's "N vacant" counts every store
without a lease, closed ones included, while the Vacant filter shows the open ones (closed sit under
Closed) — *for the owner*; "Collected by" reads the lease's channel words (Bank debit, Wire, Zelle,
Other; the drawing's "E-mail" is not a channel); the Status filter defaults to All (the drawing's
"Active" selection shows vacant and closed rows); "filtered from N" appears on the stores list only
(the siblings would need one more count query); the provider status reads as copied ("SENT", Rule
2) where the drawing reads "Open"; the "view" link opens the builder's invoice page (no provider
URL exists); "Cancel in Stax" is the existing delete-invoice action, admin only, and a one-off has no
Void; the promise card uses the operator's open promise on the lease (no field ties a promise to a
charge) and shows while the balance is open; the Activity card shows to every role (the payment page
already did), the "All activity" link to admins; the Invoices card's date column reads "Issued"
(`sent_at` is often empty); "from the lease checklist" is written only when a field says so; the
outcome links are the existing GET links (no single-submit guard needed).

**Gate 1.** ruff, `ruff format --check` (471 files), mypy strict (471 files), `uv lock --check`,
`makemigrations --check` on sqlite and PostgreSQL 16: all clean. **Gate 2.** sqlite 6,704 passed,
664 skipped (20:32) with one failure, the shell test's icon list (the chevron and the banknote
added; the pin and the plan's contract updated by Fable, the file re-run 74 passed on both
engines); PostgreSQL "Migration tests" 34 passed (16:51), "Unit tests" 7,311 passed, 3 skipped
(21:42) with the icon pin and 20 failures in Opus's `test_plan012fa_b_detail.py`, every one at
fixture setup: the tests assigned `provider_status` directly, which PostgreSQL's Rule 2 trigger
(`finance_guard_provider_status`) refuses and sqlite does not enforce (finding B-6) — fixed by Opus (the invoice's status written through `invoice_sync.record_provider_status`, the suite's pattern), the file and the shell test re-run by Fable: 97 passed on sqlite and 97 passed on PostgreSQL;
e2e 29 passed in each of four runs on PostgreSQL. Visual: the thirty baselines regenerated in the
image against PostgreSQL (34 passed), compare runs 34 passed on both engines, a sqlite regeneration
equal to the PostgreSQL baselines in all thirty files; the two stores-list baselines changed, no
other. **Gate 3.** Diff read: every list parameter passes `_one_of` against a fixed list or the
search's cleaner; the filters, the sort and the page run over every store; the balances come from
the shared receivables helper; the activity card is one query with notes masked and a bounded slice;
the "All activity" link is admin-only and keeps ISO in its query string; no production line writes
`provider_status`; every existing action keeps its URL, method and role; the two CSS blocks are
delimited and the role rules stay in the one phone block. **Gate 4.** Secret scans (script and
pattern, the diff and every new file) clean; no provider name in a new file; the new inputs are
five GET filters on an existing page, each validated against a list, none echoed when unknown
(Fable's fuzz over every control of six lists: markup, overlong, traversal, injection-shaped,
negative, NUL and encoded NUL); a viewer sees no primary action or form link on any list and no
action on a charge page, billing no Void; markup in a store's and an operator's name is escaped on
every list and detail page; the balance badge never names a wrong family; the vacant filter and the
counts line pinned as decided.

**Visual check.** B1's stores list at 1,440 reads like the drawing (counts line, one filter row,
the title-and-subline store, the balance and status badges, the chevron, the footer); B2's charge
page at 1,440 reads like the drawing (tiles, the Invoices card with the provider pill, the empty
Payments state, Activity, DETAILS, the tinted Open promise card, MORE). At 390 the list cards wrap
unevenly (B-3, open for the cleanup); the charge page stacks cleanly.

**Residual risk (B).** The sibling lists have no visual baseline yet (Batch D adds the family
pages to the suite). The Payments list's note badge and the operator page's meta-badge rule are
covered by the tag's synthetic tests, not by a rendered page with a note.

**Verdict (B):** PASS (one MAJOR in a test fixture and two MINOR resolved; B-3 open for the cleanup; the owner's questions of plan §9 stand, plus the counts-line reading of "vacant")

### Batch C — the builder and form family (D5) and the wizard frame (D8)

**Scope.** *C1 (Opus):* the invoice builder on the reference's frame: `invoices/_builder.html`
(the form layout), `_draft_form.html` as the Lines card (Item, Description, Qty, Unit amount, Total,
a trash icon button; the rent line's lease and month selects under its item; the Save-as-catalogue
label under the item; the dashed "+ Add line" with the help sentence), `_collect.html` with one
`collect-tile` per entry of `collect_form.options` (the chosen tile `is-chosen`, its fields inside
it; a sixth choice needs no template change), `_collect_note.html` (one sentence per choice),
`_summary.html` (the SUMMARY card: lines and amounts, the total, memo, due date, the five primary
buttons with their ids, Discard; the help card); `new.html` and `draft.html`'s header line and
"Draft · nothing reaches <provider>…" note; `invoice_lines.js` (the rent-column collapse removed,
the summary lines rebuilt with `textContent`); the `line_total` filter over
`invoice_builder.shown_line_total` (the rounding rule moved to the service, the view's total summing
it); the form family on the same frame (Store with Store cost and Documents cards; Lease with
Lease, Debits, Setup checklist in the Lines style and Documents; Add payment with Payment and
Allocation, the unallocated line in the summary; the charge payment forms; Operator; Calendar
entry), every field id, name, help text and error placement kept; the `trash` icon; app.css's
"Form and builder family" block, the summary sticky on a laptop and fixed above the tab bar on a
phone. *C2 (Opus):* `views/wizard_frame.py` (`frame`: step number, count, back link, the store's
title; `statement_notes`: the tiles' notes from the figures and one query for the deposit's
collection day; the settlement rows), `lease_ends/_wizard.html` (the base every step of both
wizards extends: the header with "Step N of M · nothing changes until you confirm", the 260 px step
card with done / current / later states and links, the context lines and the cancel as a red
text-link POST form, the main card with the step's title and description, the footer with ← Back
and the step's primary bound by `form=`), `_tiles.html`, `_settlement_box.html` (Deposit held, −
applied, − kept, + credit, the green refund or red owed row, both when both), the statement step's
deductions table (Reason, Note, Amount, trash; `deduction_rows.js` hides the empty rows after the
last filled one, "+ Add deduction" reveals the next, the trash clears a row; without script all
eight rows show), `_figures.html`'s compact mode; app.css's "Wizard frame" block with the phone
rules in the one phone block. *Fable:* `tests/test_plan012fa_c_adversarial.py` (6: the tiles come
from the options and a hostile choice is never echoed; a viewer's POST to the builder and every form
is refused and writes nothing; every form page has its cards and one primary in the summary; the
wizard header counts the steps and the cancel is a guarded POST; a viewer and billing cannot open
the statement step; the statement tiles read the figures).

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| C-1 | MINOR | finance/templatetags/builder.py (C1) | The `line_total` filter imported a private helper from a view module through a one-form shim | Resolved: `invoice_builder.shown_line_total` in the service, used by the view's total and the filter; a test that both agree; the dead `lines_have_rent` context value removed |
| C-2 | MINOR | tests/test_plan012fa_c_adversarial.py (Fable) | The tile test counted radios by the buttons' name | Resolved (the radios are `collect`), found by Opus |
| C-3 | MINOR | the first C1 build | A marker class `is-checked` tripped an earlier adversarial test's substring check for `checked` between inputs | Resolved by Opus before hand-back (`is-chosen`) |

**Decisions taken (Fable) and for the owner.** The confirm button keeps "Transfer the store" (the
brief kept the sentences; *for the owner*: "Confirm the transfer" as in the drawing is a copy
change); the builder's primary keeps "Record $X received" (the drawing reads "as paid"; same);
the item box keeps the existing text-and-datalist field (the drawing's bold kind + month would
need the form's choice field, out of scope); the Charge now tile does not name a threshold
("after a confirmation page"); the step circles use documented pairs (done: the paid pair; current:
the primary button's pair) rather than the drawing's solid green; the step descriptions are the
steps' existing help sentences; the "#" column, the typed deductions total and the Last month /
After the end / Credit lines stay on the statement step (pinned, informative); the Deposit tile's
"collected <day>" shows only when a settled payment gives the day; a viewer may still render the
new-operator form read-only (the route rule gates the POST; pre-existing, *for the owner* whether
the GET should refuse too); the lease-end confirmed page runs the notes helper (one read, harmless).

**Gate 1.** ruff, `ruff format --check` (476 files), mypy strict (476 files), `uv lock --check`,
`makemigrations --check` on sqlite and PostgreSQL 16: all clean. **Gate 2.** sqlite 6,758 passed,
664 skipped (20:41) with one failure, the shell's literal-colour guard on the wizard title
partial's comment ("#1991 Istanbul" read as a hex colour; C-4 below) — reworded by Opus, the file
re-run 91 passed on both engines; PostgreSQL "Migration tests" 34 passed (16:46), "Unit tests"
7,385 passed, 3 skipped (21:56) with the same pre-fix failure; e2e 29 passed in each of four runs on
PostgreSQL. Visual: the thirty baselines regenerated in the image against PostgreSQL (34 passed),
compare runs 34 passed on both engines, a sqlite regeneration equal in all thirty files; the six
baselines of the builder, the statement step and the record-payment form changed, no other.
**Gate 3.** Diff read: the tiles come from the form's options; the rounding rule lives in the
service and the view's total sums it; the wizard frame's helpers are read-only (one query for the
deposit's day); every step keeps its URL, method, ids and guards; the two CSS blocks are delimited
and the phone rules sit in the one phone block; no production line changed in forms.py or the
services beyond the shared rounding helper. **Gate 4.** Secret scans (script and pattern, the diff
and every new file) clean; the one provider mention in a new file is a test's expected rendered
sentence (tests are exempt from the Rule 8 fence by design); no new input; a hostile `collect`
value opens Save draft and is not echoed; a viewer's POST to the builder and every form is refused
and writes nothing; a viewer and billing cannot open the statement step; the cancel is a CSRF POST
with the single-submit guard; markup in a store's name is escaped in every wizard title.

| C-4 | MINOR | lease_ends/_title.html (C2) | The new comment's examples "#1991 Istanbul" tripped the literal-colour guard | Resolved: reworded |

**Visual check.** C1's builder at 1,440 reads like the drawing (Lines card, tiles with the chosen
one holding its fields, the SUMMARY card); C2's transfer statement at 1,440 matches the reference's
frame (step card, tiles, deductions with the dashed add, the settlement box with the green refund
row, Back and Save); at 390 the wizard stacks with the step line and bar and the sticky footer.
*Open (C-5, MINOR):* the Deductions tile reads the saved state while the table already shows the
suggested offset row and its typed total; a live tile update is a small follow-up for the cleanup.

**Residual risk (C).** The `(debit 2)`-style edge of the tiles' proration note, and the step
circles' counters as CSS content, are checked by markup tests, not by a reader. Statements
confirmed before 12f-a keep their old `statement_json` (unchanged).

**Verdict (C):** PASS (four MINOR resolved; C-5 open for the cleanup; the owner's copy questions stand)

### Batch D — the operator workspace (D7), the calendar (D6), the login re-shot (D9), the suite's family pages and the seven comparisons (D12)

**Scope.** *D1 (Opus):* the operator workspace to the reference: the header with the 56 px avatar
(initials, `--link` on `--month-current`, a documented pair; the drawing's `#E0F4F2` measures under
AA), the name with Active or Archived and the "Connected to <provider>" pill when a customer exists,
the muted line (phone · e-mail · SMS consent · N stores), the actions Statement · Edit · More ▾
(Connect for billing, Archive for admin; New invoice leaves the page as the spec says), the tab row
with the Store select at its right ("All stores (N)" by default; a bad or foreign value falls back to
it; the select submits on change, with the Show button without script), the strip of store cards
(rent and autopay line, the open balance in red or green, "· overdue"), the Charges card scoped to
the selection ("Charges · all stores" / "Charges · #1999 fatih"), the right column CONTACT, PAYMENT
(customer link, the linked method's nickname or type and last four, "N this year" returns, the open
promise), RECENT CONTACT (the two newest entries, Add entry, All entries), PORTAL (the statement
link's sentence, Last opened, Cancel every statement link sent; "Send invite" has no action to wire
to), then the selected store's action bar and cards; `ledger/services/workspace_cards.py` (read
helpers: initials, store cards in three queries, returns this year in one, the method record, the
open promise, the last statement-link open); `workspace._selected` and `_only_store`; budgets: the
service ≤ 10 queries by default (was ≤ 19), the page 51 with All stores and 62 with a store and a
linked method, pinned and constant. *D2 (Opus):* the calendar to the reference: the breadcrumb "Reports › Calendar", the segmented Month ·
List (`?view=list`, the existing phone list mode as a laptop choice; no Week), "Subscribe in Google
Calendar" to the feed settings; the month card's toolbar (‹ month ›, Today, Show as one select
"Everything" + each kind, Store, Operator, a no-script Show button), the grid with the day number
linking to `?on=` (today a filled circle, the selected day tinted; the billing add link on hover or
focus), chips per kind on documented pairs cut with an ellipsis, three a cell then "+N more"; the
legend of the six kinds; the side column: the day panel ("Saturday, Oct 24"; `|day_long` for
another year), NEXT 7 DAYS grouped by day, FEED (Get the link / Reset token, the existing POSTs, and
Feed settings; no Copy link: only the token's hash is stored and the URL is shown once);
`calendar.items_in(windows, filters)` (the month, the selected day and the next seven days in the
same three queries; `items()` unchanged for the ICS feed), `on_day`, `next_days`; `?on=` with a
bad or out-of-range day falling back to today with a message; the budget pinned at 15 (was 14; the
feed lookup), constant. *Fable (D12, D9):* the visual suite grows
from 15 to 25 pages (Operators, Leases and Payments lists, the calendar, the one-off charge page, a
payment page, the New store and New lease forms, the operator form, the sign-in page in a signed-out
context of its own), 50 baselines at 1,280 and 390; the report mode writes the eleven references'
shots (the four of 12f and the seven of 12f-a; the lease-end statement stands for the transfer
wizard's frame) into `docs/qa/12fa/` beside the references copied from `docs/design/`, and the
side-by-side images are composed from them; Fable's page walk and date guard cover the new pages
(the login page anonymous).

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| D-1 | MINOR | tests/test_plan012e2_b_adversarial.py (Fable) | Pinned the New invoice button on the operator page, which the spec moves to the store page | Resolved (the pin inverted, the decision cited) |
| D-2 | MINOR | the first D2 build | Two chip colours used undocumented pairs (the accessibility test refused them) | Resolved by Opus before hand-back: documented token pairs only |

**Decisions taken (Fable) and for the owner.** "All stores" is the default selection, so the
selected store's cards and bar appear after a store is picked (*for the owner*: the drawing shows a
store selected); the avatar's pair; PORTAL is labelled "Portal" with the statement-link actions
(*for the owner*: the drawing's "Send invite"); CONTACT has no Language row (no field); the
customer "open" link goes to the page's provider card (no provider dashboard URL exists, Rule 8);
the bank row shows the method's nickname or type and its last four only (Rule 1); the transfer
wizard's comparison is shot on the lease-end statement (the same frame, five steps). The calendar's day panel and NEXT 7 DAYS show kinds and titles, not amounts (calendar items carry none); the drawing's kinds that do not exist here (ACH settles, E-mail invoices, Installment, Jobs, Promise due) are not invented: the six real kinds take the drawing's colours; the Operator filter is kept though the drawing omits it; the FEED card has no Copy link (*for the owner*: a copy button would need the URL stored, which the design refuses; the URL shows once on the settings page's answer).

**Gate 1.** ruff, `ruff format --check` (479 files), mypy strict (479 files), `uv lock --check`,
`makemigrations --check` on sqlite and PostgreSQL 16: all clean. **Gate 2.** sqlite 6,816 passed,
664 skipped (12:56) with two failures, both older tests pinning the New invoice button on the
operator page that the spec moves to the store page (re-pinned by Fable with the decision cited;
118 and 75 passed on re-run); PostgreSQL "Migration tests" 34 passed (11:03), "Unit tests" 7,442 passed, 3 skipped (14:57) with the same two pins (re-run on the gate's container: 197 passed) and one failure of Fable's `test_collections_adversarial.py::test_the_feed_never_carries_a_card_number_or_a_private_marker`, which passes on PostgreSQL alone (1), in its file (19) and after the calendar files (62); the run's log kept no detail; recorded under residual risk, the Batch E gate and CI watch it; e2e 29 passed in each of four runs on PostgreSQL. Visual: the suite
grew to 50 baselines; regenerated in the image against PostgreSQL with the eleven report shots (65
passed), compare runs 54 passed on PostgreSQL and on sqlite, a sqlite regeneration equal to the
PostgreSQL baselines in all fifty files (the charge page re-shot twice to land on the data set's
damage charge, its two files checked identical again). **Gate 3.** Diff read: the operator page's
helpers are read-only (store cards in three queries, returns in one, the method record through the
existing trimmed-record reader, the promise and the statement-link open from existing rows); a
foreign or bad store value falls back to All stores; the calendar's `items_in` keeps the three
queries for several windows and `items()` unchanged for the feed; `?on=` and `?view=` validated
with fallbacks; no production line changed in the services beyond those helpers; the token file
gained one documented pair. **Gate 4.** Secret scans (script and pattern, the diff and every new
file) clean; the provider mentions in the new test files are rendered sentences and the stored
provider value (tests are exempt from the fence); no new URL; the calendar page writes nothing on
GET or HEAD (the page walk covers it); the feed token never reaches a page; a viewer sees no action
on the operator page and no add link on the calendar; markup in an operator's name is escaped in
the header and the cards.

**Visual check.** The eleven side-by-side images in `docs/qa/12fa/`: the seven references read like
the drawings at 1,440 within the facts-only decisions recorded per batch; the four 12f pages
re-shot after the dates, the role cards and the pure-GET statement. Sent to the owner.

**Residual risk (D).** One failure of the feed adversarial test in the full PostgreSQL run, not reproduced three ways (above). B-3 and C-5 stay open for the cleanup (the phone list cards' meta line;
the statement's Deductions tile not live). The suite's new pages have baselines from this run
only; CI's visual job compares them in the same image. The transfer wizard's own frame is shot
through the lease-end statement (the same template base; a transfer in the data set would need an
incoming operator and a second draft).

**Verdict (D):** PASS (two MINOR resolved; B-3 and C-5 open for the cleanup; one unreproduced PostgreSQL failure under residual risk; the owner's questions stand)

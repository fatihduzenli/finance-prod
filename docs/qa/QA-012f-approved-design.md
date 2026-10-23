## QA Report — PLAN-012f: Step 12f, the approved design applied

Gate per root `CLAUDE.md` §1.3, one section per batch. Fable runs every check itself. Engines:
sqlite and a throwaway PostgreSQL 16; e2e on PostgreSQL; the visual suite inside the Playwright
image (its baselines are regenerated once, in Batch D; until then the visual job would fail,
which is one reason nothing is pushed before the step's checkpoint).

### Batch A — tokens, Inter, the sidebar, the page header, the phone shell (D1–D5)

**Scope.** `finance/static/finance/tokens.css` (the spec's `:root` block name for name and value
for value, a test parses both; the `@font-face` for the self-hosted Inter; the `pair:` lines
rewritten for the spec with gradient stops and translucent backgrounds), `app.css`, `print.css`,
the grid page's inline styles and every template `style` attribute on the new names (a test
greps for every 12c name and for literal colours outside the tokens file; the banner carries its
two literals because the Django admin never loads the tokens file), `static/fonts/` and
`backend/finance/static/finance/fonts/` (Inter v4.1 `InterVariable.woff2`, 352,240 bytes, SHA-256
`693b77d4…9a8e3`, with the SIL OFL 1.1 text; the release zip `9883fdd4…b11e`; the one network read
approved for the step; a test refuses any external font and pins the file's hash),
`finance/templates/finance/base.html` (the 232 px gradient sidebar with the transparent logo at
48 px, the items with inline icons in the spec's order, Reports with its pages under it on its
own pages, Admin as a disclosure open on its pages, the user card with name, role, Help and Sign
out as a POST form; the banner above the page header per the spec's line; `#page-header` on the
surface with `{% block header %}` and `{% block header_search %}`; content 28/32 px, max-width
1,240 px; the phone shell: `#phone-bar` 64 px with the menu label opening the sidebar as a sheet,
the logo at 30 px and the search toggle, the banner reading "SANDBOX — TEST SYSTEM" at that
width, `#tab-bar` with four 22 px icons and 11 px labels, the active one teal, More as a
`<details>` sheet with Operators, Reports, the role's Admin pages, Help and Sign out as a POST
form; 90 px bottom padding; every target 44 px; the sheets need no script, a small script closes
them), `banner.html`, `two_factor/_base.html`, the 73 page headers moved into the header block
(the wizard title in `lease_ends/_title.html`), `finance/templatetags/icons.py` (`{% icon %}`,
twenty-one inline stroke SVGs from a fixed table), `finance/templatetags/shell.py` (`page_title`),
`accounts/templatetags/roles.py` (`role_label`), `finance/context_processors.py` (`nav_tab`, the
brand logo), `finance/pwa.py` (theme `#0b2545`, background `#f4f6f9`, tied to the tokens by a
test). Tests: `tests/test_plan012f_shell.py` (70), `tests/e2e/test_shell_12f.py` (3), thirteen
test files moved with the shell (each change listed in Opus's report), Fable's
`tests/test_plan012f_a_adversarial.py` (10) and `tests/e2e/test_fonts_12f.py` (1), two earlier
adversarial expectations updated (the sidebar label, the due token).

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| 1 | MAJOR | tokens.css (the spec's values) | Five of the spec's pairs were under AA while the spec's §5 asks AA on every pair: `--muted` on surface 4.36, `--link` on surface 3.97, `--paid-text` on `--paid-bg` 4.46, white on `--teal` (the primary button) 2.95, `--faint` on surface 2.44 (ui) | Resolved by the owner's decision: `--muted #5F6E85` (5.18), `--link #0B7C74` (5.06), `--paid-text #176B33` (5.80), `--faint` decoration only (no text uses it; a test pins that); the primary button on `#0E8F86` with a `#0B7C74` border, white on it 3.97, accepted by the owner as the one pair under AA: the contrast test pins it at its measured ratio and fails on any other pair or on this one moving; the spec §1 carries the built values with a note |
| 2 | MINOR | base.html | Settings (the calendar feed token page) left the shell with the top bar's user menu | Resolved by the owner's decision: "role · Settings · Help · Sign out" in the user card and Settings before Help in the More sheet; the bottom bar marks More on that page |

**Decisions taken (Fable) and for the owner.** The grid no longer fits twelve months at 1,280 px:
the content column is 984 px wide beside the sidebar, the table 1,180 px; the grid's scroll box
with the fixed store column and the mirrored scrollbar is the answer (PLAN-011b D2's "twelve
months at 1,280" is retired; the reference screenshot at 1,440 shows six months), and the e2e
laptop test asserts the twelve months are reachable by scrolling. The labels that follow the
screenshots (D11): "Monthly rent" in the sidebar and the bottom bar. The owner's mid-batch
addition to the spec (a Login section and `login-laptop.png`) is read and goes to Batch D with
three questions: the "E-mail" field label while usernames are not e-mail addresses, "Forgot
password?" with no reset flow in the app, "Keep me signed in" as a session change (§5 says no new
behaviour).

**Gate 1.** ruff, `ruff format --check` (552 files), mypy strict (448 files), `uv lock --check`,
`makemigrations --check`: all clean. **Gate 2.** sqlite 6,350 passed, 664 skipped (11:05); PostgreSQL "Migration tests" 34 passed
(10:10), "Unit tests" 6,977 passed, 3 skipped (12:57); e2e 21 passed in each of four runs on
PostgreSQL (the three shell cases, the font-host case and the earlier seventeen). Run on the
reworked tree (a first run on the pre-decision tree was stopped). **Gate 3.** Diff read: the tokens
file, the base template, the banner, the icons and shell tags, the context processor, the PWA
module, a sample of the moved headers. The sheets are native disclosures; Sign out is a POST in
both places; the search box is a bounded GET; the icon table is fixed markup. **Gate 4.** Secret
scans clean; the font's hash matches between the original and the served copy. Fable's cases: a
company name with markup escaped in the sidebar, the user card and the title; a viewer's sidebar
and More sheet carry no admin page; Sign out is a POST form and a GET signs nobody out; the
bottom bar marks the page it is on and only that one across five pages; the header search is a
bounded GET and a hostile query echoes nothing; the banner is one element on every page with the
title prefix kept; every font request goes to the app's own host (e2e).

**Residual risk (A).** The look is checked on markup and styles; the visual baselines are
regenerated at the end of the step, so the pixel check of the shell waits for Batch D; the
`:has()` and `color-mix()` rules need current browsers (the owner's phone and laptop are).

**Verdict (A):** PASS (one MAJOR and one MINOR, resolved by the owner's decisions in one rework cycle).

### Batch B — the component set (D6)

**Scope.** `app.css` (one section per component, tokens only: cards with header, count pill,
right link and section label; the button set with the menu "▾"; inputs and selects; the
segmented filter; the tabs with the teal active border; the seven badge families; tables with
the muted header on surface-2, `--border-soft` rows, no zebra, `td.money`, the first-cell title
and subline, the totals row, the scroll region, the sticky store column; the KPI card; the
progress bar; the date chip; the contact-log entry with its avatar; the grid cell block with the
dashed "Not yet due" and the tinted current month; the help line; the 12c toast, pager, steps,
confirm, empty and field styles restated), `print.css`, `tokens.css` (`--month-current` with its
pairs), `finance/templatetags/components.py` (`status_badge` over the families, `kpi`,
`progress`/`progress_widths`, `date_chip`, `avatar`), `ledger/_cell_title.html` and
`contacts/_log.html`/`_log_entry.html` (new; `operators/_contacts_table.html` removed),
`ledger/services/grid.py` (the cell's words, `overdue_days`, no new query), `ledger/views/dashboard.py`
(`cell_html`, `this_month`), the templates that carry the classes (lists, tabs, tables, the
grid). Tests: `tests/test_plan012f_components.py` (41), thirteen test files moved with the
markup (each change listed in Opus's report), Fable's `tests/test_plan012f_b_adversarial.py`
(14 cases) and `tests/test_plan012e4_adversarial.py` moved to the cell block.

**Badge families.** paid ✓ (paid, settled); unpaid ● (due, owed, Needs review, returned, needs
premium); overdue ● dark red with white text (a rent charge past due); pending ◐; partial ◑;
waived –; neutral ○ (voided, not yet due). An unknown status is a plain badge.

**Findings.** None open. One test-only correction by Fable: three assertions of the dependent-fields e2e case compared a button's raw `innerText`, which now reports the button's spans as lines because `.btn` is a flex box; they compare the whitespace-normalised text (the button reads as before).

**Decisions taken (Fable).** A one-off "owed" sits in the light unpaid family (the screenshot's
deposit); "Not yet due" reads in `--muted` because `--faint` is decoration only; the progress
bar's and the promise badge's colours (the screenshot's amber and orange; the yellow "Open")
are set in Batch C where those appear; the grid's cell widths are Batch C's layout.

**Gate 1.** ruff, `ruff format --check` (554 files), mypy strict (449 files), `makemigrations
--check`: all clean. **Gate 2.** sqlite 6,396 passed, 664 skipped (18:04); PostgreSQL "Migration tests" 34 passed
(15:46), "Unit tests" 7,037 passed, 3 skipped (19:26); e2e: four runs of 21 with the one
innerText failure above, then three runs of 21 passed after the test correction, all on
PostgreSQL. **Gate 3.** Diff read: the stylesheet sections, the
tags (every tag goes through `format_html`; the progress widths are floored to a tenth so they
sum to at most 100; an unknown family or avatar family raises, never renders), the partials,
the grid service. **Gate 4.** Secret scans clean. Fable's cases: the progress widths never
exceed 100 for thirds, extremes and five equal parts; a zero, negative, text, NaN or infinite
total draws no bar; an unknown family is a template error; the KPI, badge, avatar and date chip
escape markup and ignore a hostile tone; a contact entry with markup is escaped and its avatar
letter comes from the kind; the segmented filter keeps the tab's query; the grid block carries
the formatted amount and never a raw one.

**Verdict (B):** PASS (no finding; three decisions deferred to Batch C where their elements appear).

### Batch C — the dashboard, the store and operator workspaces, the monthly rent overview (D7–D9, D11)

**Scope.** `ledger/services/dashboard.py` (new: the KPI notes, the pending debits with the
five-business-day settle date, the month's collection percentage and segments on the rent
charges, the coming-up items from the leases the attention list already reads, the last five
settled payments), `ledger/views/dashboard.py`, `dashboard/home.html`, `_attention.html` (the
item's store, operator, amount and why badge from rows already read), `ledger/services/workspace.py`
(`Header.open_count`, `debit_day_text`, `deposit_text`, `next_rent`, `next_insurance`,
`lease_since`), `ledger/views/workspace_cards.py` (new), `stores/detail.html`, `_header.html`
(the side column: LEASE, NEXT, STORE COST, DOCUMENTS), `_summary_strip.html`, `_cost_history.html`,
`_tab_charges.html` (the Charges card with the segmented filter, the title and subline, the
totals row), `operators/detail.html` (the same layout with the store selector), `grid/page.html`
(the header with Download spreadsheet, the filter card, the legend, the grid card with 116 px
months and the Collected row with its counts), `ledger/services/stores.py` (`title`),
`tokens.css` (`--progress-pending`, `--progress-partial`). Query budgets: the dashboard 46 → 49
(one query each for the pending debits, the collection and the recent payments; the bound 25 +
3 × due unchanged), the grid 19, the workspace ≤ 25 and constant. Tests: `tests/test_plan012f_pages.py`
(19), twelve test files moved with the layouts, Fable's `tests/test_plan012f_c_adversarial.py`
(7) and two earlier adversarial expectations scoped to the new cards.

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| 1 | MINOR | grid/page.html | The filter card split the Status label from its select when the row wrapped at 1,440 px | Resolved: each label and control wrap as one `.filter-field`; the Show button stays with the last pair; a markup test |
| 2 | MINOR | dashboard | The attention rows and Recent payments named the store without its number while the store page and the grid read "#101 …" | Resolved: the same `stores.title` helper (one query for the attention stores, the dashboard at about 50 against its bound) |

**Decisions taken (Fable) and for the owner.** The attention row's action button reads "Open"
for every kind (the screenshot's "Add payment", "Kept", "New lease", "Debit again" would misname
where the existing links lead; *for the owner*: keep "Open", or name the button by the item's
kind where the link matches); a broken-promise row keeps its three existing actions; the pending
cell keeps "Pending · from <date>" (copy) where the screenshot shows "· ACH"; "+ New invoice" on
the dashboard leads to the stores list (no store-less builder exists); "Today HH:MM" in Recent
payments comes from the payment row's last change on the settlement day (no settled-at
timestamp exists); the collection percentage is on the month's rent charges, floored and capped
at 100; the overdue sum and the not-sent count are no longer KPI figures (the arrears page and
the not-sent card carry them). Checked by eye at 1,440 on Opus's shots: the store page and the
dashboard read like the references; the grid's filter card split a label from its select (a
MINOR, below).

**Gate 1.** ruff, `ruff format --check` (559 files), mypy strict (454 files), `makemigrations
--check`: all clean. **Gate 2.** sqlite 6,441 passed, 664 skipped (18:26); PostgreSQL "Migration
tests" 34 passed (16:03), "Unit tests" 7,068 passed, 3 skipped (19:46, the query budgets among
them); e2e 21 passed in each of four runs on PostgreSQL. Run on the final tree after the two
MINORs (an earlier run was stopped). **Gate 3.** Diff read: the dashboard service (read
queries, no write, the settle date by business days), the workspace fields, the templates, the
grid page. **Gate 4.** Secret scans clean. Fable's cases: an empty dashboard renders every card
with a sentence and no broken number for every role; more collected than billed never shows
over 100 %; markup in a store, an operator and an address is escaped on the dashboard, the store
page, the contacts tab, the operator page, the grid and the arrears page; a viewer sees the
attention rows but no action button while billing sees one; a store without a number or an
address keeps a sensible header; a viewer's dashboard carries no link to a form.

**Verdict (C):** PASS (two MINOR resolved; one label question for the owner).

### Batch D — every other page, the sign-in layout, the baselines, the report (D10, D12, D13)

**Scope.** Ninety-five templates under `ledger/templates/ledger/` (every list, detail, form, confirm
and wizard page: the page header's title block with the breadcrumb "Stores › #NNNN › <page>" via
`_store_crumb.html` and `crumb_query`, each block a `.card`, lists in `.table-card`, filters in
`.filter-card`, forms in `.form-card` with `.form-actions`, facts in `.detail-list`), the help pages,
`finance/settings.html`, `finance/templates/404.html` (new: the shell for a signed-in user, the
sign-in panel otherwise), `accounts/403.html` (the same split), `two_factor/_base.html` (the
two-panel sign-in layout: `#auth-brand` on `--login-gradient`, `main#auth-main`, the banner as the
rounded strip), `two_factor/core/login.html` (new: the package's wizard with the fields one per row,
"Username", the full-width primary, no forgot-password or keep-me control), `two_factor/_wizard_actions.html`
(new), `app.css` (+115 lines: the cards, the breadcrumb, the form card, the wizard's phone step
line, the sign-in panels), `print.css` (cards print as plain sections; the breadcrumb and the step
line do not print), `tokens.css` (`--login-gradient` and its four pairs), `components.py`
(`{% step_line %}`), `views/invoices.py` (`crumb_store`, `crumb_operator`). Tests: Opus's
`tests/test_plan012f_d_pages.py` (19), `tests/e2e/test_pages_12f_d.py` (5), six existing test files
moved with the markup; Fable's `tests/test_plan012f_d_adversarial.py` (9) and one earlier
adversarial expectation (`test_plan012e2_c_adversarial.py`: the 404 wears the shell, so it carries
the shell's scripts and no others). Baselines: all 30 regenerated in the Playwright image; the four
report shots written by the visual suite's `--report-dir` mode into `docs/qa/12f/`, and the four
side-by-side images composed from them beside the references.

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| 1 | MAJOR | stores/_tab_charges.html, app.css (phone) | At 390 px the Charges card's rows are the generic label-and-value cards (Charge, Due, Status, Amount, Paid, Balance, Actions as seven lines) where DESIGN-SPEC §6 and store-workspace-phone.png show a two-line card: title and amount, then due and kind with the status badge | Open against Batch C's acceptance line ("the phone store page at 390"); assigned as slice D-2 (below); not a Batch D change |

**Decisions taken (Fable) and for the owner.** The login's brand sentence drops the screenshot's
"517 stores" (a made-up figure); the primary button keeps the package's "Next" (the flow's word on
the token step too; the owner may ask for "Sign in" on the first step); "Cancel" is a text link
under the buttons where the package offers one; the package's disabled "Back" on the first step is
left out. The 404 and 403 of a signed-in user render inside the shell (sidebar, banner, page header);
anonymous ones in the sign-in panel. The operator statement's Print button keeps its inline handler
(pre-existing; no CSP is set). The billing user's store screenshot has no Lease menu: the menu is
admin-only (PLAN-012e), the reference was drawn as an admin.

**Gate 1.** ruff, `ruff format --check` (562 files), mypy strict (457 files), `uv lock --check`,
`makemigrations --check` on sqlite and PostgreSQL 16: all clean. **Gate 2.** sqlite 6,470 passed, 664 skipped (20:55), with one failure: the Rule 8 fence test walked a `backend/.mypy_cache` that Fable's own mypy run had created mid-gate (gitignored, untracked, created 12:41; deleted; the fence file re-run 6 passed); PostgreSQL "Migration tests" 34 passed (17:56), "Unit tests" 7,098 passed, 3 skipped (20:59), the three D-2 markup cases failing as intended (the slice is not built; they define finding 1); e2e 26 passed in each of four runs on PostgreSQL, the two D-2 browser cases failing likewise. The flaky
`test_every_control_on_the_main_pages_has_a_label` (one failure in Opus's eight runs on a tree
mid-edit): six isolated runs of the file green (14 passed each), green in every full run; not
reproduced, kept under residual risk. **Gate 3.** Diff read: the permission guards of the templates
(every `can_*`, `csrf_token` and `is_admin` occurrence present at HEAD is present after, several
more where a page split into cards); no `|safe`, no `autoescape off`, no inline style added but the
pre-existing search width; the 404 shows nothing from the request; the sign-in page reflects
nothing. **Gate 4.** Secret scans (`scripts/check-no-secrets.sh`, the pattern scan of the diff and
the new files) clean; no secret-store read. Fable's cases: the login page keeps its one POST form
with the CSRF token, the field names, the "Username" label, no forgot or keep-me control, one banner
and the invitation sentence; a hostile `next` (protocol-relative, absolute, javascript:, markup,
backslash) is never reflected outside an escaped attribute; a signed-in viewer's 404 and 403 carry
the shell and one banner; a breadcrumb never carries raw markup and a viewer sees no primary
action in any page header; every main page renders for every role with the sidebar, the tab bar and
one page header.

**Visual check (D12).** The four built shots beside the references, `docs/qa/12f/*-side-by-side.png`:
the dashboard, the store workspace and the monthly rent overview read like the references at
1,440 (the known copy differences of Batch C: "Open" on the attention rows, "Pending · from <date>"
in the grid, the earlier blocks below the dashboard as cards). The phone store page matches the
shell, the header, the three summary tiles, the actions and the segmented filter; its charge rows do
not (finding 1).

**Verdict (D):** PASS for Batch D's own scope; the step carries finding 1 (MAJOR, Batch C's phone
criterion) until slice D-2 is gated.

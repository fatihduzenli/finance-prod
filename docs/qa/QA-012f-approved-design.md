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

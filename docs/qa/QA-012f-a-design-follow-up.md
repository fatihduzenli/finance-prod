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

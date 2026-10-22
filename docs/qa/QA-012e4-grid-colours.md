## QA Report — PLAN-012e-4: the grid's colour families, blank cells, the store column (follow-up F3)

Gate per root `CLAUDE.md` §1.3. Fable runs every check itself. Engines: sqlite and a throwaway
PostgreSQL 16; e2e on PostgreSQL; the visual suite inside the Playwright image.

**Scope.** `finance/static/finance/tokens.css` (Paid green unchanged 7.08; Unpaid dark red
`#B42318` with white ink 6.57; a new soft unpaid pair `#FCD9D6`/`#8A1C12` 7.11 for a charge not
yet overdue; Pending yellow `#FFF1B8`/`#5C4500` 8.03; Partly paid light orange `#FFDCC0`/`#8A3B00`
6.0; Waived grey unchanged 6.35; the due tokens and the unpaid border removed; every pair line and
the measured-ratios paragraph updated), `app.css` (the badges on the same tokens), `ledger/services/grid.py`
(a `due` cell reads "Unpaid $X" like `unpaid` with its own glyph; a lease span per row: months
before the first lease's start month or after the last lease's end month are blank with a
month-only label; "Not yet due" only for a future month inside a lease; a cell with a charge is
never blank; `GridRow.store_title`: "#1991 Istanbul", "#2922 TUCSON" when the name starts with
the number, "#2475 <city>" or "#2475" when the name is the number, the name alone without a
number), `grid/page.html` (the cell styles; the legend Paid, Unpaid not yet overdue, Unpaid
overdue, Pending, Partly paid, Waived, Not yet due; the store cell: the title link, then the
operator; the state and the channel left the cell). No migration, no stored value changed. Tests:
`tests/test_grid.py` (49, with the span, the title and the legend cases), seven test files
updated for the text "Unpaid", the legend, the blanks and the labels. Fable's:
`tests/test_plan012e4_adversarial.py` (7 cases).

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| 1 | MINOR | grid.py `store_title` | A name that starts with its number read "#2922 2922 TUCSON"; an imported store whose name is only its number read "#2475" with no name | Resolved: the number is not repeated; the city stands in when the name is only the number; tests for seven forms |

**Decisions.** The partial colour moved to a light orange so it stays apart from the new yellow
pending (the plan said "as today"; accepted); future months inside a lease now count as `not_due`
for the status filter and the column counts; a blank month inside a lease keeps the "no charge"
label and no `aria-label` attribute, a month outside it the month-only label; the charge pages'
label for a `due` charge is unchanged (the TextChoices label).

**Gate 1.** ruff, `ruff format --check` (543 files), mypy strict (442 files), `uv lock --check`,
`makemigrations --check`: all clean. **Gate 2.** sqlite 6,262 passed, 664 skipped (19:05); PostgreSQL "Migration tests" 34 passed
(17:15), "Unit tests" 6,889 passed, 3 skipped (19:33); e2e 17 passed in each of four runs on
PostgreSQL. **Visual.** In the Playwright image the
compare against the previous baselines moved the grid pages and the store Charges tab (the badge
colours) and nothing else; regenerated and compared twice: 34 passed. Checked by eye: the legend
without "Due", the two unpaid shades, yellow pending, the store cell with the title and the
operator, blank cells before a lease and "Not yet due" in the future months inside it.
**Gate 3.** Diff read: the tokens, the badges, the service, the template. **Gate 4.** Secret
scans clean. Fable's cases: a store name with markup escaped in the store cell and the number
leading; a charge due today in the soft shade and one due yesterday in the dark shade, both
"Unpaid"; months before the start and after the end blank, future months inside "Not yet due";
two leases on one store leave no gap; a charge outside the lease dates is never blank; the legend
has no "Due" item and two unpaid shades; the badges and the cells share their tokens and no due
token remains.

**Verdict:** PASS (one MINOR resolved).

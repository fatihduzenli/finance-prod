## QA Report — PLAN-012e-3: "Check" as a manual payment method; the builder's lines table in a scroll region

Gate per root `CLAUDE.md` §1.3. Fable runs every check itself. Engines: sqlite and a throwaway
PostgreSQL 16; e2e on PostgreSQL; the visual suite inside the Playwright image.

**Scope.** `ledger/models.py` (`PaymentMethod.CHECK`, `MoneyMethod.CHECK`, `MANUAL_PAYMENT_METHODS`,
the manual-method constraint built from the constant), migration `ledger/0048_check_method.py`
(three choices-only `AlterField`s and six CHECK constraints recreated with the value; the reverse
refuses while a row stores `check`), `ledger/services/rent_run.py` (`MANUAL_METHODS` Zelle, wire,
check, other), `ledger/services/lease_end.py` (a refund by check recorded as a `check` payment),
`ledger/forms.py` (`MANUAL_METHOD_CHOICES` built from the service constant; the reference label
switching to "Check number" while Check is chosen; the help naming the check number; the deposit
resolution offering Check), `ledger/services/reports.py` and `csv_export.py` (a "Check (USD)"
column in payments by method), `payments/offline_marking.py` (`METHOD_WORDS` gains check: the one
change in `payments/`, approved in writing by Fable; the memo reads "Paid by check on …"), the
payment templates loading the dependent-field script, `invoices/_draft_form.html` (the lines table
in the scroll region), `shown_when.js` (a label switch, `data-label-when`/`data-label`),
`app.css` (`.table-scroll { position: relative }` so the visually-hidden labels stay inside the
region). Tests: `tests/test_check_method.py` (22), the round-trip and `sqlmigrate` cases of 0048,
three e2e cases (the label switch with and without script; `scrollWidth == 1280` on the draft
page before and after Add line), updates in eight test files. Fable's: `tests/test_plan012e3_adversarial.py`
(12 cases); the frozen-value list of PLAN-012c updated for `PaymentMethod` and `MoneyMethod`
(the deliberate change).

**Findings.**

| # | Severity | Where | Defect | Outcome |
|---|---|---|---|---|
| 1 | BLOCKER | payments/offline_marking.py | `METHOD_WORDS` had no "check": the mark-paid job of every check payment raised `KeyError` and failed (found by Opus, who could not edit `payments/` without approval) | Resolved with written approval: the word added; a test records a check through the builder, runs the worker, and asserts the mark reaches the provider as cash with the check number and the memo "Paid by check on …" |
| 2 | MINOR | ledger/services/lease_end.py | A lease-end refund by check was recorded as "other" | Resolved: mapped to `check`; two assertions in `test_lease_end.py` moved from Other to Check |
| 3 | MAJOR | invoices/_draft_form.html, invoice_lines.js | Inside the scroll region at 1,280 px only Line, Item and the two empty Rent columns were visible; Description, Quantity and Unit amount sat off-screen (found on the regenerated baseline) | Resolved: the Rent columns collapse while no line is Rent and open when one is (the script toggles `has-rent` and `hidden` on `.rent-col`; without script the columns show and the region scrolls); a markup test and an e2e case (the unit amount within the viewport, the columns opening on Rent, `scrollWidth` 1,280) |

**Decisions.** The migration recreates six constraints (the plan's "no-op" was wrong and is
corrected in the plan); the CSV method columns write the stored key as they do for every method;
the payments-by-method report gains a Check column; `MoneyMethod` gains the value (lease credits
and deposit resolutions); payments recorded as "other" before stay as they are.

**Gate 1.** ruff, `ruff format --check` (540 files), mypy strict (441 files), `uv lock --check`,
`makemigrations --check` on sqlite and PostgreSQL 16: all clean, exit 0. **Gate 2.** sqlite 6,241 passed, 664 skipped (17:46); PostgreSQL "Migration tests" 34 passed
(15:40, with the 0048 round trip and its `sqlmigrate` cases), "Unit tests" 6,868 passed, 3 skipped
(19:00); e2e 17 passed in each of four runs on PostgreSQL (the three new cases included). Run on
the final tree after the second rework cycle (a first run on the cycle-1 tree was stopped and
discarded).
**Visual.** In the Playwright image the compare against the Step 12e baselines moved exactly the
builder pages (1,280 px wide now, 2,031 before; the phone page shorter) and the Record a payment
page (the new help line); regenerated and compared twice: 34 passed. **Gate 3.** Diff read: the
model and migration, the two services, the forms, the script, the templates, the report. The
label switch is a convenience over the no-script label; no form lists methods by hand (a test
greps). **Gate 4.** Secret scans clean. Fable's cases: a check number that is a card number or a
routing-like run is refused with nothing written and never shown on the ledger or the store page;
a check number with markup is refused by the text guard (or escaped, had it been stored); the
builder's record path writes the same rows and the same number of audit rows for a check as for
Zelle; a method outside the manual set (`ach`, `credit`, `cash`, `CHECK`, `check `, markup) is
refused by the service and the form with nothing written; the lines table sits inside the scroll
region on both builder pages.

**Residual risk.** The mark-paid memo for a check reaches the provider through the fake in tests;
the owner's click on staging ("Record payment now" by check, then the invoice paid in Stax with
the memo) is the check against the real API.

**Verdict:** PASS (one BLOCKER, one MAJOR and one MINOR, resolved in two rework cycles).

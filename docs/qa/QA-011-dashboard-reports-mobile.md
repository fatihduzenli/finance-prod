## QA Report — PLAN-011: dashboard, month grid, arrears, reports, exports, mobile and PWA; waive fix

**Date:** 2026-10-06 · **QA owner:** Fable · **Implementer:** Opus
**Plan:** `docs/qa/PLAN-011-dashboard-reports-mobile.md` · ADR-0009 addendum (Waive and a live invoice)

**Scope.** `backend/ledger`: services `receivables` (one `open_balances` behind the dashboard,
aging, arrears and the statement), `grid`, `reports`, `csv_export` (one writer, a reviewed header
registry of 150 labels), `one_off_charges` (waive refusal, "Delete provider invoice" remedy,
dashboard list), `rent_run.due_counts`; views `dashboard` (home `/`, grid, arrears), `reports`
(menu, aging, revenue, payments by method, deposits, statement), export routes on every list;
migration 0022 (five indexes); 29 templates made responsive. `backend/finance`: `pwa` (manifest,
service worker with a static-only allow-list and a content digest version), `middleware`
(`Cache-Control: private, no-store` on every dynamic response), `templatetags/responsive`,
`static/finance/{app.css,icons/*}`, `templates/finance/sw.js`, two-factor base template override,
`LOGIN_REDIRECT_URL = "/"`. `backend/payments`: `one_off_invoices.delete_waived`, the
`invoice_delete` runner flag, `PROVIDER_LABEL`. Registry rules. No infrastructure change, no
Stax change, no new dependency (icons generated with the standard library).

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0, 0 | clean, 360 files |
| `uv run mypy` (strict) | n/a | 0 | 293 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | PostgreSQL 16 | 0 | 4,246 passed, 3 skipped |
| same | sqlite | 0 | 3,764 passed, 485 skipped |
| `uv run pytest -m e2e -q` (Chromium; includes the 390×844 phone flow) | PostgreSQL 16 | 0 | 6 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan over the diff (incl. hostnames) | n/a | 0 | clean; the two PNG icons are 192×192 and 512×512, 0.5 and 2.3 KB |

**Tests.** 3,944 → 4,246 on PostgreSQL. Opus: `test_waive_live_invoice` (22), `test_dashboard`
(16), `test_grid` (35), `test_arrears` (17), `test_baseline_aug_2026` (6), `test_report_indexes`,
`test_csv_export` (46), `test_reports` (43, with a 200-round property test that aging equals the
dashboard's outstanding), `test_list_exports`, `test_report_performance` (PostgreSQL),
`test_pwa` (37), `test_responsive` (30), `test_documents_camera` (10), e2e `test_mobile`.
QA-owned `test_reports_adversarial.py` (21): **the service worker executed under Node** against
fake requests (navigations, `/grid/`, a CSV, `/healthz`, a document download, a POST, a
query-string static URL and a foreign origin are never answered or stored; only the allow-listed
static GET is answered, and even it is not stored when the response is private/no-store;
activate deletes other caches), the precache list names static files only, an independent
random-data check that aging equals outstanding (12 seeds), formula safety of the new CSVs with
hostile store, operator and state names, the remedy route closed to viewer/billing/no-role/
anonymous, hostile grid/arrears/aging/dashboard parameters → 200, and statement isolation
between operators.

**Measured (PostgreSQL, 500 stores, 12,000 rent charges, 10,000 invoices, after ANALYZE):**
dashboard 0.18 s, grid 500×24 0.57 s, arrears 0.14 s, aging 0.10 s, revenue 0.55 s, payments by
method 0.03 s, deposits 0.07 s, statement 0.08 s; query counts fixed per page (the dashboard's run
summary no longer grows with leases due today: 450 leases due, bounded).

**Indexes added (migration 0022):** `rent_charges (status, due_on)` arrears and overdue;
`payments (settled_on, status)` paid this month and the method report; `invoices (provider_status)`
failed attempts; `one_off_charges (operator_id, status)` arrears and aging;
`insurance_charges (lease_id, due_on)` arrears and aging. A sixth, `rent_charges (lease_id,
period_month)`, was dropped as the left prefix of the existing unique constraint.

**August 2026 baseline (fixture):** the grid's August column shows 392 collected for
$1,470,001.57, 47 unpaid, 13 pending; arrears 40 operators; the dashboard and run summary agree.
The same check on the migrated data is recorded after Step 13.

**Findings.** BLOCKER 0. MAJOR 1, resolved: the dashboard's run summary ran three queries per
lease due today (grouped counts now). MINOR 3, resolved: a redundant index; the payments CSV
`note` header; `Note` in the QA test. Design choices accepted: `PROVIDER_LABEL` so the ledger
never names Stax; the grid CSV's "Store number" and "Channel" placement where the mapping did not
pin them; the reconciliation and audit exports keep their page roles; aging is a snapshot as of
today; the manifest and the worker require a signed-in user; the precache allow-list comes from
the static finders (whitenoise's storage writes no manifest). **Deferred by written decision:**
`TIME_ZONE = "UTC"` puts a 23:59 New York settlement on the next day (owner's calendar decision);
the ledger "Deposits held" total over the filtered set (QA-010b).

**Residual risk.**
- Installing on a real phone (iOS Safari and Android Chrome), the camera `capture` attribute,
  HEIC from a camera roll and the sticky first column on iOS were not exercised; Playwright ran
  Chromium at a phone viewport. The manifest and worker need a signed-in session; if a phone's
  install prompt fetches the manifest without credentials the install still works from the
  browser menu with the page's icon and title.
- The staging image serves static files from `STATIC_ROOT`; the allow-list is built from the
  finders and is verified there by the owner's install.
- Print rendering of the statement is asserted by CSS presence only.
- The Google Sheet push is not built (owner amendment 7).

**Verdict: PASS** (pending the staging done-when on a laptop and a phone).

## QA Report — PLAN-010b: setup fees, store costs, deposits, profit and loss

**Date:** 2026-10-05 · **QA owner:** Fable · **Implementer:** Opus
**Plan:** `docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md` · **Decision record:** ADR-0007

**Scope.** `backend/ledger`: models `FeeType`, `SetupCharge`, `DepositResolution`, `StoreCost`,
`InsurancePremium.cost`, `Payment.setup_charge` (+ kinds `setup`, `refund_deposit`),
`Invoice.setup_charge`; migrations ledger 0012–0014 (schema, seeds, store costs and deposits);
services `setup_charges`, `deposits`, `store_costs`, `pnl`, shared `ChargeKind` in `rent_run`,
`leases.end_lease`, `payments_ledger` (deposits held, public CSV helpers); views `setup_charges`,
`deposits`, `reports`, additions to `leases`, `stores`, `payments`; forms, URLs, admin, templates;
command `import_store_costs`. `backend/payments`: job kinds `setup_payment_mark`,
`setup_payment_retry_mark` (migrations 0008, 0009), the transaction/invoice/dispute webhook
handlers and the refunds, returns and reconciliation consumers of the new payment target.
`accounts/registry.py`. Dependency `openpyxl==3.1.5` (MIT) + `et-xmlfile==2.0.0` (MIT), pinned,
imported by one module only (test-enforced). Files: owner template copies, two-row examples
(`docs/templates/examples/`, identical copies under `backend/tests/fixtures/store_costs/` because
`docs/` is not in the image), test fixtures. Docs: ADR-0007, RUNBOOK §11, DATA_OWNERSHIP, README.
No Stax call added; no infrastructure or CI change.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0, 0 | clean, 290 files |
| `uv run mypy` (strict) | n/a | 0 | 230 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 3,232 passed, 1 skipped (sqlite-only), 5 deselected |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 2,783 passed, 450 skipped (PostgreSQL-only) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |
| Pattern scan over the full diff and new files; `.xlsx` files inspected as plain zips (9–10 members, < 21 KB unpacked) | n/a | — | no hit |

**Tests.** 2,769 → 3,232 on PostgreSQL. Opus: `test_setup_charges.py` (68), `test_store_costs.py`
(121), `test_deposits.py` (41), `test_pnl.py` (70), `test_end_lease.py` (29), plus model, guard
and migration round-trip extensions. QA-owned `test_setup_fees_adversarial.py` (25): role sweep
over every new write route for viewer, no-role and billing; P&L safe methods only; malformed and
foreign identifiers → 404; hostile prices through the checklist (huge, NaN, Infinity, hex,
thousands separator refused and nothing saved; scientific notation and other digit scripts
stored as the exact amount); card numbers in vendor/note/import cells never stored or echoed;
import dry run with refusals writes nothing; a formula in an imported description is inert and
the CSV export is formula-safe; header-only and empty files; a future-dated cost row never
changes today's margin; transferred deposits held once; a refund above paid refused; the P&L
counts no deposit, voided payment or waived charge; hostile P&L parameters → 200. P&L
performance: 500 stores × 12 months in about 0.2 s and 12 queries on PostgreSQL.

**Findings.** BLOCKER 0. MAJOR 1, resolved: no screen could end a lease, so deposit resolution
was unreachable (End lease action added, admin, autopay must be paused first). MINOR 7,
resolved: no retry for a failed setup-charge mark; fragment assertions on "4111"; deposits-held
total counted a transferred deposit twice (found by the QA test); the ledger counted withheld
amounts as held while the P&L did not (one shared rule now); private CSV helpers imported across
modules; test helpers bypassed the real end-lease path. MINOR 1, **deferred by written
decision**: the payments-ledger "Deposits held" total is over the filtered set, not a balance at
a date (the P&L footer is; documented in the module and ADR-0007). Design departures accepted:
an ACH `setup` payment is refundable like rent (the plan's D4 wording would have forbidden it);
`first_year_insurance` seeded inactive (ADR-0007 §3, flagged to the owner); a waived charge's cost
still counts in fee costs; store costs attributed to the lease active on the 1st; withholding only
the whole deposit (a partial refund records the remainder as withheld).

**Residual risk.**
- The owner has not yet confirmed the first-year-insurance choice (ADR-0007 §3); activating the
  type without a rule change would bill year one twice.
- The importer was not tested against a workbook saved by Excel itself (cached formula values
  were produced by editing openpyxl output); the owner's filled template is the first real file.
- No browser (e2e) test covers the new screens; the owner's done-when clicks are the browser check.
- Step 13 (sheet import of legacy balances, removal of `store_balances`) is documented, not built.
- Insurance appears under "fees" in the P&L (billed by `due_on`, cost from the premium); the owner's
  column list did not name insurance, so this is a reading to confirm.

**Verdict: PASS** (pending the staging done-when: checklist on lease creation; landlord rent and
margin; wire payment on a sign fee; two-row import; P&L row and CSV; deposit refund at lease end).

---

### Addendum, 2026-10-05: the first CI run after the push hung in the unit tests

**What happened.** CI run 37342732918 on commit `bd34846` was cancelled by the 30-minute job
timeout in the unit-test step; nothing was deployed. The same suite had passed twice locally on
PostgreSQL 16 and once in a Linux container.

**Diagnosis (reproduced, not guessed).** Looping the affected files in a one-CPU Linux container
reproduced the hang about one round in three. The stack dump showed the main thread waiting on
PostgreSQL inside `ledger/services/pnl.py::_payments`, called by
`test_500_stores_by_12_months_in_under_two_seconds`. `pg_stat_activity` showed the query
**active with no wait event** (executing, not locked), and `pg_class.reltuples` was 0 for
`payments`, `rent_charges` and `leases`: autovacuum had analysed those tables earlier in the
session while they were empty, and the test's 6,000 rows per table sat uncommitted in its
transaction. The planner therefore joined them by nested sequential scans. Whether autovacuum
runs at that moment is a matter of timing, hence the intermittence.

**Fix (test only).** `busy_stores` in `backend/tests/test_pnl.py` now runs `ANALYZE` on the
tables it loads before anything is measured, as a live database's statistics would be current.
Verified by the same loop that reproduced the hang (see the table below). CI hardening
(verification configuration): the unit-test step now passes `-o faulthandler_timeout=300` and has
its own 15-minute limit, so any future hang names its test.

**Finding.** [MINOR, deferred by written decision] `ledger/services/pnl.py` — the report's
queries run without a statement timeout. Failure scenario: planner statistics badly out of date
on a live database (for example right after a bulk import and before autovacuum's analyze) could
make `/reports/pnl/` run for minutes while holding a web thread. Required change, for the next
hardening plan: a `statement_timeout` around the report's queries with a value-free "try again"
message, and an `ANALYZE` of the affected tables at the end of the bulk importers. Not a
correctness defect; the figures are unaffected.

**Verdict unchanged: PASS**, on the re-run gate below.

| Command (re-run after the addendum's changes) | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .`, `uv run mypy` | n/a | 0 | clean, 291 files; 230 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | PostgreSQL 16 | 0 | 3,234 passed, 1 skipped |
| same | sqlite | 0 | 2,785 passed, 450 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 3 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |
| Reproduction loop: the four affected files, Linux container, one CPU, 20 rounds | PostgreSQL 16 | 0 | 20 clean rounds (before the fix: a hang about one round in three) |

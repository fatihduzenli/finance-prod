## QA Report — PLAN-010d: one-off charges and invoices from the app; Charge now; the audit log page

**Date:** 2026-10-05 · **QA owner:** Fable · **Implementer:** Opus
**Plan:** `docs/qa/PLAN-010d-one-off-charges-and-audit-page.md` · **Decision record:** ADR-0009 ·
docs/CLAUDE.md Rule 2 amended by the owner's decision (pay-an-invoice for "Charge now" only).

**Scope.** `backend/ledger`: `setup_charges` → `one_off_charges` (model `OneOffCharge`, categories,
collections, description, operator FK, void fields, `charge_now_key`; migrations 0017 schema, 0018 row
rewrite, 0019 constraints, 0020 guard function for bidi marks, 0021 audit indexes); services `one_off_charges`
(creation path for every category, cancel, Charge now request/confirm), `audit` (page, CSV,
Activity), `validators.visible_text` (bidi and format marks), `leases` (method snapshot readers);
views `one_off_charges` (new-charge forms on operator and lease pages, cancel, Charge now
confirmation), `audit`; templates; registry; nav. `backend/payments`: `one_off_invoices` (worker:
create with check-before-create, send, schedule with one occurrence, delete, pay), job kinds
`invoice_create|invoice_send|invoice_delete|schedule_create|schedule_delete|invoice_pay` (migrations
0010 schema, 0011 rows, 0012 constraints, 0013 invoice_pay), `InvoiceRecord.reference|schedule_id`, `ScheduleInput.reference`, the adapter's
`RetryPolicy.NONE` for `pay_invoice`, `FakeProvider` additions, the invoice webhook handler's
linking by reference and schedule. No infrastructure change, no new dependency.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0, 0 | clean, 327 files |
| `uv run mypy` (strict) | n/a | 0 | 263 source files |
| `uv lock --check` | n/a | 0 | lock current |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | PostgreSQL 16 | 0* | 3,934 passed, 3 skipped |
| same | sqlite | 0* | 3,454 passed, 483 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 5 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |
| Pattern scan over the diff and new files | n/a | — | no hit |

\* The first full run reported one failure, `test_stax_isolation.py::test_no_file_outside_payments_mentions_stax…`,
caused by a stray `backend/.mypy_cache` directory left by a tool run, not by any file of the change;
the directory was removed and the test passes (6/6). No code changed after the full runs.

**Tests.** 3,704 → 3,934 on PostgreSQL. Opus: `test_one_off_charges.py` (82), `test_charge_now.py`
(25), `test_audit_page.py` (30), bidi cases in `test_invisible_notes.py` and `test_card_number_guard.py`,
every 10b file updated for the rename with no assertion weakened. QA-owned: `test_one_off_adversarial.py`
(21: hostile descriptions and notes, role sweep for viewer, billing, no-role and anonymous, a webhook
with our reference but another total, cancel after a pending payment, Charge now on a card method,
two confirms and the immutable key, hostile audit parameters, prefix-filter isolation and CSV
content), `test_stax_adversarial.py` rewritten for the one-request rule of `pay_invoice`,
`test_setup_fees_adversarial.py` carried over the rename.

**Findings.** BLOCKER 0. MAJOR 0. MINOR 4, resolved: `visible_text` ignored bidi and format marks
(found by Opus; the database guard function was replaced to match); the audit page crashed on
extreme years (found by the QA tests); a line-length violation in a view docstring; the P&L query
count rose to 13 for the arrears mapping (accepted as a documented change, not a defect).
Design choices accepted: `deposit_resolutions.setup_charge` keeps its column name; `operator`
nullable only for a legacy setup row on an operator-less lease; check-before-create runs only
after a recorded attempt; cancel keeps its note in the audit row and re-checks before deleting;
edit refused for non-setup charges (cancel and recreate instead); the invoice due date is the
later of the charge's due date and today; PDF-less details as reported. **Still deferred by written
decision:** the ledger "Deposits held" total over the filtered set (QA-010b).

**Residual risk.**
- Nothing ran against Stax. Unverified until ACH is enabled on the sandbox: that `meta.reference`
  returns on GET (the check-before-create falls back to total and date), that a one-occurrence
  schedule behaves as assumed, whether a deleted invoice reads `DELETED` or 404 (both handled),
  the shape of the pay-an-invoice response (if it lacks `method`, the payment appears through the
  webhook only, and the job says so). The owner's staging clicks cover the email, manual, cancel
  and wire-payment paths; "Charge now" and `autopay_due` are proven by unit tests until then.
- A pre-existing `bounded()` log message still names the P&L when the audit page times out.
- The audit and documents date filters count New York days while `TIME_ZONE` is UTC; shown on
  the page.
- No concurrency test for two creates or two cancels in parallel threads (row locks and the
  active-job rule cover it by reasoning).

**Verdict: PASS** (pending the staging done-when after the push).

---

### Addendum, 2026-10-06: the first push failed at the staging migration; fixed and re-gated

**What happened.** CI run 37397383935 on `4b855f9`: tests green; the web revision
`finance-staging-web--0000031` failed activation because `deploy_migrate` raised
`cannot ALTER TABLE "audit_log" because it has pending trigger events` in
`ledger.0017`. The transaction rolled back; the database stayed at `ledger.0016` and revision
0000030 (10c) kept serving. The CI job had already rolled the worker and the reconcile job onto
the new image before the web wait, so both ran new code against the old schema until the next
deployment (staging only; the worker refuses unknown job kinds and the reconciliation would have
failed loudly).

**Cause, reproduced.** A data migration updated `audit_log` rows twice in one transaction; a row
with an actor queues the deferred foreign-key check that Django's `DEFERRABLE INITIALLY DEFERRED`
constraints create, and the later `ALTER TABLE audit_log ENABLE TRIGGER` in the same transaction
is refused. The migration tests ran on empty tables and could not see it. The same shape existed
in `payments.0010` (`jobs` rows with `requested_by` followed by `AddConstraint`) and would have
failed next.

**Fix.** Both migrations split into schema → rows → constraints (`ledger` 0017/0018/0019,
`payments` 0010/0011/0012; the later ones renumbered to 0020, 0021 and 0013). The row migration
disables the append-only trigger before any update and runs `SET CONSTRAINTS ALL IMMEDIATE` before
re-enabling it, inside one transaction, forward and reverse (accepted: the audit log is never left
writable across a commit). **New test** `test_the_rename_migrates_over_existing_rows_and_back`
seeds representative rows at the pre-0017 state through the historical models and round-trips;
it failed against the old migrations with the staging error and passes now. **CI**: the deploy job
now updates the web app, waits until a web revision on the commit is Healthy and Running, and only
then updates the worker and the reconcile job; a web failure leaves both untouched.

**Also in this addendum.** Three remaining fragment assertions (`"4111" not in …`) on pages or
session hashes were tightened to the full number after one of them flaked on a random hash
(logged as a flake, treated as a failure, fixed in the test).

| Command (re-run after the fix) | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .`, `uv run mypy` | n/a | 0 | clean, 332 files; 267 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | PostgreSQL 16 | 0 | 3,939 passed, 3 skipped (includes the seeded migration round trip) |
| same | sqlite | 0 | 3,458 passed, 484 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 5 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |

**Verdict unchanged: PASS**, on this re-run.

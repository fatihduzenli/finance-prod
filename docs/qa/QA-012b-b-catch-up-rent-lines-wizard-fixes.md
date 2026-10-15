## QA Report — PLAN-012b-b: rent run catch-up, rent billed by invoice, lease-end wizard fixes, P&L at lease end, splitting rent

**Date:** 2026-10-15 · **QA owner:** Fable · **Implementer:** Opus · **Plan:**
`docs/qa/PLAN-012b-b-catch-up-rent-lines-wizard-fixes.md` · **Decision:** ADR-0012 §7

### Batch A — catch-up (D1–D3), the draft's lifecycle (D9), the final rent charge (D10), the unpaid deposit (D11)

**Scope.** `backend/ledger` (`services/rent_run.py`: `create_month`, `catch_up_lease`,
`debit_date`, `ChargeRunReport.caught_up`; `leases.py` create/update call the catch-up;
`lease_end.py`: `DEPOSIT_UNPAID`, `in_progress`, `store_in_progress`, `adopt_for_transfer`,
`open_statement`, `sweep_drafts`; `statements.py`: `OpenLine.url`, `DepositLine.charged/text`;
`transfer.py`: adoption, the "not charged yet" branch dropped; views `lease_ends`, `transfers`,
`stores`, `run`; templates; `management/commands/collections_tick.py`: charge creation and the
draft sweep; `models.py` `LeaseEnd.statement_opened_at`; migration 0040), tests (two new files;
fixtures of `make_lease` and the rent-run lease builder now avoid the first charge on save by
saving before the start, with explicit starts where a test needs an earlier month; no test
skipped or deleted).

**Decisions in the gate.** (1) The daily job never created rent charges before this batch (only
the button did); the owner's text says "daily job and the button", so `collections_tick` now
calls `create_charges` first. **For the owner:** the first tick after the deploy creates every
missing month (bounded to 24) for every active lease with a start date; on staging that is test
data. (2) A deposit from the setup checklist cannot be cancelled, so the blocker reads
"…not collected: collect it, or waive or cancel the charge." (3) The due date of a charge made
by the wizard follows the last-month rule (the debit day, or the end date when earlier, never
before a start in the same month).

**Commands run (final Batch A code).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 474 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 383 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 1 | 5,075 passed, 620 skipped, 1 failed: Fable's `test_setup_fees_adversarial` deleted leases that now carry their first charge (test fixed: the charges go first); the file re-run green on both engines; 6:52 wall |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 22 passed; 5:13 wall |
| the "Unit tests" selection | PostgreSQL 16 | 1 | 5,670 passed, 3 skipped; the 13 failures were the same setup-fees test and the 12 cases of Fable's Batch B adversarial file, written ahead of Batch B and parked until it lands; 9:31 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `1e1ed40` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_rent_catch_up.py` (16), `test_lease_end_drafts.py` (14), the
tick's charge-creation cases, seeded round trip of 0040, updated expectations in
`test_proration`, `test_run_views`, `test_rent_run`, `test_transfer`. Fable
(`test_plan012b_b_a_adversarial.py`, 5 + 1 PostgreSQL-only): the catch-up never creates a charge
for another lease's month or past the end and is idempotent; two runs at the same moment create
each charge once (threads, PostgreSQL); an adopted draft is bound to its own store, blocks no
transfer and the plain wizard defers to the transfer; a viewer sees the banner without the
Cancel link and gets 403 on it; the final charge is one of 31 days whatever the deductions post
and never doubles; the deposit blocker is not cleared by a part payment and clears when paid.

**Findings (A).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 1 | decision | plan D3 | No daily tick created charges; the plan assumed one | Resolved: the tick creates charges first (owner's text), tested |
| 2 | MINOR | `lease_end.DEPOSIT_UNPAID` text | "cancel the charge" is not possible for a setup-checklist deposit | Resolved: "collect it, or waive or cancel the charge" |

Design choices accepted: the fixture changes (leases saved the day before their start);
`debit_date` (a start-month debit before the start is due on the start date); the statement's
due-date rule; the 24-month bound stated on the run page.

**Verdict (A):** PASS (the one failure was Fable's own test fixture; no production finding beyond the two above).

### Batch B — rent billed by invoice (D4–D8), the P&L at lease end (D12)

B_SECTION

### Batch C — splitting rent (D13)

C_SECTION

**Confidentiality gate (§5.4).** Secret scans clean on every gate; no secret-store read by either
model; no new credential-capable file type; no new secret name.

**Verdict:** FINAL_VERDICT

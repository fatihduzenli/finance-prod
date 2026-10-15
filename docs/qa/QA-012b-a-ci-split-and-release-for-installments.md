## QA Report — PLAN-012b-a: the migration tests as their own CI step; a split that releases the parent's Stax object

**Date:** 2026-10-14 · **QA owner:** Fable · **Implementer:** Opus (part 2); Fable (part 1,
verification-only CI config) · **Plan:** `docs/qa/PLAN-012b-a-ci-split-and-release-for-installments.md`

### Part 1 — CI step split (Fable)

**Scope.** `.github/workflows/ci.yml` (the test job's unit step split into "Migration tests"
and "Unit tests"), `docs/BACKLOG.md` §4. No production, dependency or test change.

**Measurement (PostgreSQL 16, local, 2026-10-14, `--durations=80` on the whole "not e2e" set:
21:44 wall).** The 21 migration tests account for about 500 s (8:20; `test_migrations_roundtrip.py` alone timed at 6:41) of it; the rest about
800 s (13:24). CI ran the undivided step in 22:11 on the 12b push, so the CI service runs
at about local speed.

| Check | Result |
|---|---|
| YAML parses; the step list reads Lint, Format check, Type check, Migration tests, Unit tests, Playwright, End-to-end tests, Secret scan | ok |
| The two selections are complements: all 5,657 "not e2e" tests collected = 21 (migration step) + 5,636 (unit step), the 9 sandbox tests deselected in both as before | ok |
| Timed local run of the migration step's selection on PostgreSQL | 21 passed in 8:04 (exit 0) |
| Timed local run of the unit step's selection on PostgreSQL | 5,633 passed, 3 skipped in 13:58 (exit 0): under the owner's 15 minutes |
| Limits: migration step 15 (≥ 1.5 × measured), unit step 20 (≥ 1.4 × measured; the owner's requirement is the measured time under 15) | ok |

**CI confirmation (run 38680501084, push of `a59de59`, 2026-10-15):** Migration tests 10:34
(limit 15), Unit tests 12:44 (limit 20; under the owner's 15 minutes), e2e 0:36, deploy green.

**Residual risk.** The real CI timing was seen on that run; the limits carry the
margin above. A new migration test must be added to the migration step's list by hand (the
comment in the workflow says so); a forgotten one simply runs in the unit step.

**Verdict (part 1):** PASS (verified by complement counts and timed local runs; the CI timing is confirmed on the next push).

### Part 2 — the split releases the parent's Stax object (Opus)

**Scope.** `backend/ledger` (`services/installments.py`: `release_kind`, `release_notice`,
`release_pending`, `_request_release`, `after_release`, the refusals `LIVE_INVOICE_SHARED` and
`SCHEDULE_FIRED`; `services/one_off_charges.py`: `mark_invoice_released`,
`mark_schedule_released`, `released_invoices`, `parent_paid_on_plan`, `plan_status` with the
second cause, `apply_plan_status`, `lock_plan_parent`, `retry_params`, the refusals
`CANCEL_PARENT_ON_PLAN` and `WAIVE_PARENT_ON_PLAN`; `services/attention.py` kind
`split_charge_paid`; views `installments`, `one_off_charges`, `invoices`; templates `split`,
`plan`, `one_off_charges/detail`, `invoices/draft`), `backend/payments` (`one_off_invoices.py`
`release_invoice`/`release_schedule`; `jobs/runners.py` dispatch on `params.release`), tests. No
migration, no new job kind, no dependency, no environment variable; every provider call is one
the adapter already made (`delete_invoice`, `delete_scheduled_invoice`, the listing).

**Commands run (final code of both parts).** Env as in QA-012e; PostgreSQL 16 in a throwaway
container; the PostgreSQL run split exactly as the new CI steps are.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 468 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 379 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 5,039 passed, 618 skipped; 11:27 wall |
| the "Migration tests" selection | PostgreSQL 16 | 0 | 21 passed; 8:04 wall |
| the "Unit tests" selection | PostgreSQL 16 | 0 | 5,633 passed, 3 skipped; 13:58 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `c3f2b2f` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_installments.py` (+13 release cases: invoice and schedule
release end to end with one provider delete, the local invoice void/DELETED, the charge owed at
its price, the installments' objects created after; the multi-line and fired-schedule refusals;
rent unchanged; a provider error with Retry; a payment before the release → attention, item,
cancel; the release run twice; a plan cancelled before the release; lease-end blockers clear;
`invoice_line_links --check`; page texts; +8 rework cases: cancel and waive of a parent on a
live plan refused in service, page and routes, allowed again after cancel or completion, the
release still runs), `test_attention.py` (+3). Fable (`test_plan012b_a_adversarial.py`, 6
cases): a release job acts on its own entity whatever its parameters name (the other charge's
invoice stays live, one delete call with the right reference); two release jobs queued at once
still queue the installments once with one audit row; the parent cannot be split again while
its release is pending; a viewer sees no split page, Retry or plan action; a late provider
payment on the released invoice lands on the parent only, the plan goes to attention with the
item and nothing is voided; a released and split charge is counted once in the P&L and the
ledger, equal to the unsplit one paid the same way.

**Findings (part 2).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 1 | MAJOR | `one_off_charges.cancel_refusal`, `waive` | A parent whose plan is live could be cancelled (voided by a plain delete, reachable after a failed release) or waived on its own while its installments stayed owed (Opus's own report) | Resolved: both refused with "cancel the plan" sentences, buttons hidden, routes refuse; the release path never passes through the cancel rule; tests |

Design choices accepted: the lock order installments → parent → plan everywhere (the
parent-payment hook needs the parent first); `plan_status` reads payments, the attention item
reads stored values (one query) and ignores a stale status; the release outcome sentences; the
schedule release audited as its own action; a schedule that fires between the split and the
release fails the job with its sentence and staff cancel the plan.

**Residual risk.**
- Real Stax behaviour of a delete on a sent invoice and of the listing after a schedule delete
  is exercised only on staging (the owner's click: a split of an e-mailed charge whose invoice
  is live, then the Stax dashboard shows it deleted and one invoice per installment).
- The lock order under real concurrency (a cancel racing a parent payment) is reasoned, not
  tested.
- The split of a rent charge with a live invoice still needs the invoice cancelled first (by
  design, rent invoices are cancelled without voiding the charge).

**Verdict (part 2):** PASS (one MAJOR resolved in one rework cycle; residual risk as listed).

**Confidentiality gate (§5.4).** Secret scans clean; no secret-store read by either model; no
new credential-capable file type; no new secret name.

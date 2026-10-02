## QA Report — PLAN-010: database job queue, worker container app, nightly reconciliation

**Date:** 2026-10-02 · **QA owner:** Fable · **Implementer:** Opus
**Plan:** `docs/qa/PLAN-010-worker-and-reconciliation.md` (D5 amended during QA, see Findings)
**Decision record:** ADR-0006 in `docs/DECISIONS.md` (database queue, no Celery, no Redis)

**Scope.** Backend: new `payments/jobs/` (queue, runners), `payments/reconciliation/` (run,
steps a–e, lock, schedule gate, mismatches), management commands `worker`, `reconcile`,
`age_pending_payment`; `payments/models.py` (`jobs`, `reconciliation_runs`,
`reconciliation_mismatches`); migrations `payments/0005_jobs`, `0006_job_kinds_reads`,
`0007_reconciliation`; views `ledger/views/{jobs,reconciliation}.py` and every view that used
to call the provider (`operators`, `leases`, `charges`, `payments`, `run`, `webhooks`) now
queues a job; `ledger/services/leases.py` (payment-method snapshot), `rent_run.py`
(`defer_mark`, `mark_provider_invoice`); `payments/webhooks/{receiver,processor}.py` (the
in-process thread of PLAN-008 removed, `queue.py` deleted); `payments/alerts.py` (two fatal
events); `payments/stax/limiter.py` (departure counter); `finance/{env_guard,settings}.py`
(six D12 settings); `accounts/registry.py`; `ledger/{forms,urls,validators}.py`; templates.
Infra: `infra/modules/containerAppWorker.bicep` (new), `infra/modules/containerAppJobReconcile.bicep`
(new), `infra/main.bicep`, `infra/parameters/staging.bicepparam` (`staxCaller` back to `none`,
web replicas back to the default 2). CI: worker and job image updates with tolerance; the
10-minute verification step. `Dockerfile` comment. `.env.example` (names only). Docs:
ADR-0006, RUNBOOK §9–10, DATA_OWNERSHIP §3, STAX_CLIENT §5, README, infra/README,
TODO-VERIFY-059..061.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0, 0 | clean, 269 files |
| `uv run mypy` (strict) | n/a | 0 | 211 source files, no issues |
| `az bicep build --file infra/main.bicep` | n/a | 0 | no warnings |
| `uv run python backend/manage.py makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 2,769 passed, 1 skipped (sqlite-only lock case), 5 deselected |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 2,361 passed, 409 skipped (PostgreSQL-only) |
| `uv run pytest -m e2e -q` (Playwright, chromium) | PostgreSQL 16 | 0 | 3 passed |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |
| Pattern scan over the full diff and every new file (keys, tokens, connection strings, private keys, high-entropy strings) | n/a | — | no hit |

**Tests.** 2,533 → 2,769 on PostgreSQL (2,154 → 2,361 on sqlite). The four tests of Prompt 10
item 5 pass without mocking the limiter: a deleted local payment is restored from the
transaction list; a wrong local invoice status is overwritten and logged; a failure in any step
(parametrised a–e) leaves the cursor and records the step; 500 invoices through the real
adapter, transport and limiter on a fake clock never depart more than two in any second.
QA-owned adversarial files: `test_reconciliation_adversarial.py` (new, 24 cases: hostile text in
mismatch and note rows, resolve gates and 404s, vanished invoices, corrupted and future cursors,
the pending threshold on the Friday/Sunday/Monday boundary, Run now against the night's job,
the fixture command's argument edges) and `test_operators_adversarial.py` /
`test_rent_run_adversarial.py` now drain the queue after each request. Static and runtime checks
in `test_jobs.py` prove no view module names a provider-reaching function and no enqueueing
view makes a provider call. Suites were run twice on each engine during QA with no
intermittent failure; one pre-existing weak assertion (`"4111" not in page`) was tightened.

**Findings.** BLOCKER 0. MAJOR 1, resolved: the scheduled Container Apps Job ran the
reconciliation itself, a second Stax caller with its own limiter; D5 amended so the job only
queues a `reconcile_run` row (`STAX_CALLER=none`, no boot check) and the worker runs it under
the advisory lock. MINOR 4, resolved: no busy check on enable/pause/resume (double click queued
two jobs); no termination grace period on the worker (now 60 s); a stale Dockerfile comment; a
resolution note of invisible characters counted as a note (`visible_text` helper; refused).
MINOR 1, **deferred by written decision**: the same invisible-character weakness exists in the
pre-existing mandatory notes (manual payment, return resolution, link-invoice with a differing
total); not introduced by this change; to be fixed with the shared helper in the next small plan.
Opus design choices reviewed and accepted: the automatic methods refresh after a sync is not
the operator page's job state; `settled_payment_undeposited` waits 3 days after settlement;
provider-only objects get a derived UUID as mismatch entity (no provider ID stored); an orphaned
transaction reference is removed (audited) before a payment is restored; rejections naming no
local payment are counted only; a replay refused by a misconfigured worker still counts one
attempt on the event (unreachable in deployment: the worker refuses to start without
`STAX_CALLER=worker`).

**Residual risk.**
- Stax response shapes for deposits and ACH rejections are unverified until live data
  (TODO-VERIFY-030, 060, 061); step (c) may produce noise mismatches that staff resolve.
- The pre-run budget estimate counts invoices plus three list pages; deposit detail reads
  and extra transaction pages are not estimated. At most a few dozen requests a night.
- A reconciliation longer than the worker's 60-second grace period at a revision swap is
  interrupted; the run row is marked `Abandoned` on the next run and the job is re-queued
  once. Deploys at 02:00 New York are not expected.
- Bicep compiled only; the Container Apps job trigger, `terminationGracePeriodSeconds` and
  the CI verification step are exercised on staging after the deployment.
- The daily summary email (Prompt 10 item 3) waits for the Phase 7 email provider; Sentry's
  fatal rule is the alert until then (ADR-0006).
- ACH-dependent proofs (restored settlement, missed return) remain blocked on Stax ACH.

**Verdict: PASS** (pending the staging done-when: operator edit through the worker within a
minute; Run now shows counts; the aged pending payment triggers the Sentry email; the
scheduled job's run appears the next morning).

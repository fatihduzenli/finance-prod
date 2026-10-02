# PLAN-010 — Database queue, single worker, nightly reconciliation

**Date:** 2026-10-02 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Inputs:** Prompt 10 and the owner's amendments (2026-10-02, which win on conflict); ADR-0002,
ADR-0004 (Redis deferred to this step); `docs/STAX_CLIENT.md`; `docs/RUNBOOK.md`; `docs/DATA_OWNERSHIP.md` §3;
`docs/inputs/STAX_ENDPOINTS_2026-09-30.md` (transactions `GET /transaction` with `start_date`,
`end_date`, `per_page`, `page`; deposits `GET /query/deposit` and `/query/depositDetail` by date and
`batchId`; ACH rejections `GET /query/statement/v3/ach-rejects` by date range); the owner's CI request
of 2026-10-02.

## 1. Restated objective

Every Stax call moves out of the web process into one worker container app that polls the
database: web actions that need Stax (operator create and edit, link, enable, pause and resume
autopay, mark a manual payment paid, retry marking, refund, void) create a **job row** and show
its outcome on the page with the same single manual retry; webhook events are processed by the
same worker from the rows the receiver already stores. The nightly reconciliation runs as an
Azure Container Apps **scheduled Job** invoking a management command under a lock row: it
re-fetches open and recently changed invoices, lists transactions since the last success minus a
48-hour overlap, lists deposits for 14 days and matches them, fetches ACH rejections and returns
for the window, flags pending payments older than five business days, writes a report row,
advances the cursor only on success, alerts on every mismatch through Sentry at level fatal, and
shows everything on a reconciliation page with "Run now" for admins. No Celery, no Redis.

## 2. Stop conditions, answered

| Condition | Answer |
|---|---|
| Platform job time limit shorter than the run | Container Apps Jobs take a configurable `replicaTimeout` (set to 3,600 s here). 500 invoices plus transactions and deposits at 2 requests per second is under ten minutes. Chunking is not needed; the job is resumable anyway (D9). |
| Deposit endpoints too thin to match transactions | `depositDetail` by date and `batchId` is expected to list the transactions of a deposit (TODO-VERIFY-030); unobservable until ACH exists in the sandbox. Deposit matching is built and marked **unverified until live data** (Checkpoint 10 warning). Not stopped on. |
| Amendment 1: a requirement the database queue cannot meet | None found: one worker replica keeps the limiter and breaker in one process; a database queue with row locks gives at-least-once delivery, which every job here tolerates because each is idempotent (replay keys, external_refs checks, re-fetch before write). Latency of a few seconds is acceptable (owner: within a minute). Recorded as **ADR-0006** (Fable). |

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | New table `jobs`: `id`, `kind` (closed list), `entity_type`, `entity_id`, `params` (jsonb, IDs and amounts only, guarded), `status` (`queued`, `running`, `done`, `failed`, `dead`), `attempts`, `max_attempts` default 1 (jobs never auto-retry; the manual retry creates a new job), `result` (text, class and field names or the outcome sentence), `requested_by`, `created_at`, `started_at`, `finished_at`, `locked_until`, `replay_key` (unique nullable; carries the form's creation key). Claimed with `SELECT … FOR UPDATE SKIP LOCKED`; a job left `running` past `locked_until` (worker died) is re-queued once and then `dead` | Amendment 1 and 2 |
| D2 | Management command `worker`: a loop that, each tick (1 s idle, no sleep when work found), claims one job or one unprocessed webhook event (jobs first), runs it through the existing service function, records the result, and exits cleanly on SIGTERM. One process, one thread; the in-process webhook thread of PLAN-008 is removed. `process_webhooks` stays as the manual catch-up | One caller, one bucket |
| D3 | Web views stop calling services that reach the provider. Each such view validates, writes the local change (as today: local first), creates a job, and redirects; the page shows the job state ("Queued", "Running", "Done: <outcome>", "Failed: <class> (fields)") from the latest job of that entity, with a meta refresh of 5 s while queued or running, and the existing retry button, which creates a new job. The services keep their signatures and run unchanged inside the worker | Amendment 2; no change to tested business logic |
| D4 | Worker container app `finance-<env>-worker` (Bicep module): same image, command `python backend/manage.py worker`, one replica (min and max 1), `STAX_CALLER=worker`; web gets `STAX_CALLER=none` and `webMaxReplicas` back to 2; `provider_environment_check` keeps running on both at start (boot path bypasses the caller gate). The breaker, budget and limiter state tables are shared through `sync_state` already; only the worker process owns the in-memory instances | Amendment 2 |
| D5 | Scheduled job `finance-<env>-reconcile` (Bicep, `Microsoft.App/jobs`, trigger cron `0 6,7 * * *` UTC, parallelism 1, `STAX_CALLER=none`): runs `python backend/manage.py reconcile --scheduled`, which applies the local-time gate (the local time in `America/New_York` is inside the two-hour window that opens at 02:00 and no scheduled run or queued scheduled job exists for that window; the second firing covers daylight-saving time) and then **queues a `reconcile_run` job**; a firing that is not due records a `skipped` row. The **worker** runs the reconciliation, so one process still owns the limiter, the breaker and the budget (docs/CLAUDE.md Rule 3); the job container never calls Stax. "Run now" queues the same job kind with trigger `manual` and ignores the gate. The manual `reconcile` command (no flag) runs directly and only inside the worker container. Container Apps cron is UTC-only; this gives 02:00 New York all year. *Amended 2026-10-02 after Batch B: the first draft ran the reconciliation inside the job container, which made a second Stax caller with its own in-memory limiter.* | Amendment 1; owner's done-when |
| D6 | Lock: `pg_try_advisory_lock` on a fixed key around the whole run, plus a `reconciliation_runs` row in `running`; a second start (scheduled or Run now) records `skipped: already running` and exits. Local sqlite: a `sync_state` row lock | Amendment 1 and 5 |
| D7 | The run, in order, each step through the rate-limited client, paced by the limiter (no bursts; the report records duration and request count): (a) re-fetch every local invoice with `provider_status` in `DRAFT, SENT, VIEWED, PARTIALLY APPLIED, PARTIALLY PAID, ATTEMPTED` or updated in the last 7 days; apply through the sync writer; a local status that differs is overwritten and logged as a mismatch `invoice_status_differs` (resolved automatically, listed for the record); (b) `list_transactions` from `sync_state.reconcile_cursor − 48 h` to now, paged; upsert payments through the Prompt 8 transaction handler logic; a local payment missing → restored and logged `payment_restored`; (c) `list_deposits` for 14 days and `get_deposit` per batch; match to settled payments by transaction ID; unmatched deposit rows and unmatched settled payments older than the deposit window → mismatches; (d) `list_ach_rejections` for the window; each rejection matched to a payment; a rejection we never saw by webhook → apply the return path and log `return_missed_by_webhook`; (e) pending payments older than `PENDING_PAYMENT_MAX_BUSINESS_DAYS` (default 5, US weekends excluded) with no webhook → `pending_too_old`; (f) write the report row; advance `reconcile_cursor` to the run's start time **only when every step completed** | Prompt 10 items 1 to 2; amendments 3 and 4 |
| D8 | Alerts: one Sentry event at level **fatal** per run that found at least one new mismatch (fingerprint `reconciliation-mismatch`, counts by kind, no IDs), and one at level fatal for a failed run (`reconciliation-failed`). The daily summary email of Prompt 10 item 3 depends on the Phase 7 email provider; until then the reconciliation page is the daily report and the Sentry rule is the alert. Recorded as a known gap | No email provider yet |
| D9 | Resumable: each step is idempotent and the cursor advances only at the end; a run that stops halfway leaves the report row `failed` with the step reached; the next run redoes the window | Prompt 10 item 2 |
| D10 | Tables `reconciliation_runs` (`started_at`, `finished_at`, `trigger` scheduled/manual, `status`, `step_reached`, counts checked/matched/mismatched/restored per step, `requests`, `duration_ms`, `error`) and `reconciliation_mismatches` (`run`, `kind`, `entity_type`, `entity_id`, `detail` (guarded text, no IDs unmasked), `resolved_at`, `resolved_by`, `resolution_note`). Page `/reconciliation/`: last run, counts, mismatch list with "Resolve with note" (billing), "Run now" (admin, same lock) | Amendment 5 |
| D11 | Test fixture command `age_pending_payment --payment <id> --days N`: sets `initiated_on` back N days on a pending payment, audited, refused in production (`APP_ENV=production`). This is what the owner runs on staging to trigger the alert | Owner's done-when |
| D12 | Configuration: `PENDING_PAYMENT_MAX_BUSINESS_DAYS=5`, `RECONCILE_LOOKBACK_DAYS=35`, `RECONCILE_OVERLAP_HOURS=48`, `RECONCILE_DEPOSIT_DAYS=14`, `RECONCILE_LOCAL_TIME=02:00`, `RECONCILE_TIMEZONE=America/New_York`; env guard, `.env.example`, Bicep env on the job | Amendment 3 |
| D13 | CI: after `az containerapp update` of the web app, also update the worker app and the job's image; then a verification step polls up to 10 minutes until the active web revision's image tag equals the commit SHA and its state is healthy and running, and the worker's active revision likewise; the job passes on that evidence even if an update command returned a transient error (each update is `|| true` followed by the verification), and fails when a revision is not live | Owner request of 2026-10-02 |

## 4. Work breakdown

| Batch | Task |
|---|---|
| A | `jobs` table and model; `worker` command (claim, run, record, SIGTERM); job kinds wired to the existing services; web views enqueue and show job state; webhook thread removed; `process_webhooks` kept; Bicep worker module; web back to `none` and 2 replicas; CI update and verification step; tests |
| B | Reconciliation: tables, `reconcile` command with lock, steps a to f, thresholds, Sentry events, page with Run now and resolve, `age_pending_payment`; Bicep scheduled job; tests including the four of Prompt 10 item 5 (deleted payment restored; wrong local status overwritten and logged; mid-run failure leaves the cursor; 500 invoices within the limiter, real limiter with a fake clock) |
| C | README; docs by Fable: ADR-0006, RUNBOOK (worker, job, reconciliation page, fixture command), DATA_OWNERSHIP §3 rows, STAX_CLIENT §5 update |

## 5. Test plan (additions by Fable)

Job claimed by two workers at once → one runs it (PostgreSQL); a job whose worker dies mid-run
is re-queued once then dead; a web view never calls the provider (AST and runtime test over all
enqueueing views); replay key on a job stops a double submit; `age_pending_payment` refuses in
production and never touches a settled payment; the cursor never advances after a failure in each
step (parametrised); the lock holds against a concurrent Run now; the page shows no unmasked IDs;
the local-time gate runs exactly once a day across the DST change dates of 2026 (8 March, 1
November); a run with 500 invoices and the real limiter departs at most 2 per second.

## 6. Acceptance

Gate green both engines; migrations clean; the four Prompt 10 tests pass without mocking the
limiter; Bicep compiles; on staging after the infra deployment: an operator edit reaches Stax
through the worker within a minute; Run now completes and shows counts; the aged pending payment
is flagged on the next run and the Sentry email arrives; the scheduled job's run appears the next
morning.

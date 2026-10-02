# RUNBOOK.md — operating the Stax integration

**Date:** 2026-10-01. Companion to `docs/STAX_CLIENT.md` (limiter, breaker, alerts) and
`infra/README.md` (Azure). Every step here that changes a live system is a Publish action under
the root `CLAUDE.md` §6 and needs the owner's word in the moment.

## 1. Webhooks: how they work

- Stax posts events to `https://<app host>/api/webhooks/stax/<token>/`. The token is
  `STAX_WEBHOOK_URL_TOKEN` from the environment's Key Vault, 64 or more random characters.
- The receiver compares the token in constant time, answers **404** with an empty body to
  anything that is not an exact match (and to GET), stores the redacted event with a
  deduplication hash, answers **200**, and hands the event to a worker thread in the same
  process. The request never calls Stax.
- The processor re-fetches the object from Stax by ID through the rate-limited client and
  applies it. **The body is never trusted**; handlers receive only the event type and the ID.
- A processing failure records the error class and the field names, counts an attempt and
  stops. After three failed attempts the event is **dead-lettered**: a CRITICAL log line, a
  Sentry event with fingerprint `webhook-dead-letter`, and a banner on the run page.
- `python backend/manage.py process_webhooks` processes any stored event that is not yet
  processed and not dead-lettered, oldest first. Run it after a restart or when the banner shows
  failed events; it is also what Step 10's worker will call.

## 2. Registering the webhooks (staging first, live at Step 16)

The app registers its own webhooks; nothing is typed into the Stax dashboard, and nothing is
registered from a laptop.

1. The environment's `APP_BASE_URL` must be the public HTTPS host and
   `STAX_WEBHOOK_URL_TOKEN` must be set in the vault.
2. With the owner's approval, run inside the container:

   ```
   az containerapp exec -g finance-sandbox -n finance-staging-web \
     --command "python backend/manage.py register_webhooks"
   ```

   The command lists the existing registrations, creates only the missing ones for the fifteen
   events below, stores each registration ID in `external_refs`
   (`entity_type = webhook_registration`), and prints event names and masked IDs. It never
   prints the URL.
3. Events registered: `create_customer`, `update_customer`, `create_invoice`, `update_invoice`,
   `send_invoice`, `create_scheduled_invoice`, `update_scheduled_invoice`,
   `delete_scheduled_invoice`, `create_transaction`, `update_transaction`,
   `update_transaction_settled`, `create_deposit`, `create_dispute`, `update_dispute`,
   `update_payment_method`. Stax exposes no separate ACH-return event; a return arrives as
   `update_transaction_settled` with a non-settled result.
4. To remove them (before rotating the token or at cutover): `unregister_webhooks`, same way.
5. **Rotating the token:** set the new value in the vault, deploy so the app reads it, run
   `unregister_webhooks` **before** the deploy with the old value if possible, then
   `register_webhooks` after. During the gap Stax's deliveries get 404 and are retried by Stax
   or caught by the nightly reconciliation (Step 10).

## 3. Checking a delivery

- `/webhooks/` lists events with type, source of the event name (header, body, shape),
  outcome, attempts and duplicates; `/webhooks/<id>/` shows the redacted payload.
- To prove the endpoint from outside: a POST to the URL with a wrong token must return 404
  with an empty body and add no row to `/webhooks/`.
- Replay (billing): re-runs the processor on a stored event; the result must be the same
  state. A replay is how a failed event is retried after the cause is fixed.

## 4. When the dead-letter banner shows

1. Open `/webhooks/`, filter by outcome `dead_letter`, open the event.
2. Read the error class and field names. `ProviderRejected` with field names means the
   re-fetch was refused; `ProviderUnavailable` or `TransportError` means Stax or the network;
   `CircuitOpen` means the breaker was open (see `docs/STAX_CLIENT.md` §6).
3. Fix the cause, then **Replay**. Do not replay in a loop; one click, read the outcome.
4. If the object no longer exists at Stax the outcome is `ignored: not at the provider`; that
   is final.

## 5. Returns queue

`/returns/` lists ACH returns (payment `returned`) that nobody has resolved. Each shows the
return code and reason as Stax reported them. Resolving needs a note. **Nothing re-debits by
itself**: a new debit needs the operator's authorisation and is a staff action in a later
prompt.

## 6. Payment states

`pending` (debit created, money not yet moved; the sheet's "9/9 pending") → `settled` (the
charge is paid) or `returned` (the charge is reopened, the return goes to the queue);
`voided`; a refund is a new payment row marked as a refund and linked to its parent. A charge is
never paid on creation.

## 7. Manual payments and the Stax side

Record a wire, Zelle or other payment on the charge page with a mandatory note. If the charge has
a Stax invoice and the payment covers the whole balance, the app re-fetches the invoice, marks it
paid in Stax as **cash** with the memo "Paid by <method> on <date>. <note>" (the only offline
method Stax offers that fits; the real method stays in our ledger), and re-fetches again. If Stax
already showed it paid, nothing is sent and the charge is flagged for review. If the Stax call
fails, the payment stands, the charge shows the failure, and "Retry marking" appears after a
minute. Partial payments are local only for now and flagged.

## 8. Refunds and voids (admin only)

From a payment's page: Refund (full or partial, up to the remaining refundable amount) or Void
(whole payment). Each has a confirmation page naming operator, store, invoice, amounts, and a
checkbox that must be ticked. The call goes to Stax, the transaction and invoice are re-fetched,
a refund becomes a new payment row linked to its parent, a void marks the original payment voided,
the charge is recomputed, and the audit log names the admin. Billing and viewer users cannot open
these pages. Refunds of manual payments are not possible here; they were never Stax transactions.


## 9. The worker: every Stax call goes through it (PLAN-010)

Since Prompt 10 no web request calls Stax. A screen action that needs the provider
(operator save, retry or link; enable, pause or resume autopay; record a manual payment
that marks the Stax invoice; refund; void; "Refresh methods" on the enrolment page;
"Match invoices" on the rent run; "Replay" on a webhook event; "Run now" on the
reconciliation page) writes its local change, queues a row in the `jobs` table and shows
**Payment provider request: Queued** on the record's page. The page refreshes itself every
five seconds until the row reads **Done: <outcome>** or **Failed: <class> (fields)**. The
same manual retry buttons as before create a new job; a job never retries by itself.

The worker is the container app `finance-staging-worker` (one replica, no ingress,
`STAX_CALLER=worker`, same image as the web app). It takes one job at a time, oldest
first, then one stored webhook event never attempted, then sleeps a second. It is the
only process allowed to call Stax, so the limiter, breaker and failure budget have one
owner.

| Symptom | Where to look | What to do |
|---|---|---|
| A page stays on **Queued** for more than a minute | `az containerapp revision list -n finance-staging-worker -g finance-sandbox` and the worker's logs | The worker is down or restarting. A new revision after a deploy takes about a minute. The job waits; nothing is lost. |
| **Stopped: the worker stopped twice while running this request** | The worker's logs around the two times | The job is `dead` after two interrupted runs. Check the record's state in the app and in Stax, then use the manual retry if the action is still wanted. |
| **Failed: ProviderNotPermittedHere** | The worker app's environment | `STAX_CALLER` is not `worker` on the worker app. A misconfiguration; nothing was sent. |
| Many jobs failing with the same class | `/status/` (breaker and bucket), Sentry | Treat as §6 of `docs/STAX_CLIENT.md`: an open breaker fails every job fast until the half-open probe succeeds. |

Manual catch-up of failed webhook events is still `process_webhooks` (§3); the worker takes
only events never attempted.

## 10. The nightly reconciliation

The Container Apps scheduled Job `finance-staging-reconcile` fires at 06:00 and 07:00 UTC
and runs `reconcile --scheduled`, which **only queues** a `reconcile_run` job when the
local time in America/New_York is between 02:00 and 04:00 and no scheduled run or queued
scheduled job exists for that window; one of the two firings is 02:xx all year, the other
records a `skipped` row that the page hides. The worker then runs the reconciliation under
a PostgreSQL advisory lock (a second start is recorded as `skipped: already running`).

Each run, in order and through the limiter: (a) re-fetch every open or recently changed
invoice and overwrite a local status that differs (`invoice_status_differs`, resolved by
the run); (b) list transactions from the cursor minus 48 hours (35 days on the first run)
and apply each through the webhook handler's own path, restoring a payment we lost
(`payment_restored`); (c) deposits of the last 14 days against settled payments
(`deposit_unmatched`, `settled_payment_undeposited`); (d) ACH rejections of the window,
applying a return no webhook reported (`return_missed_by_webhook`); (e) pending payments
older than five US business days (`pending_too_old`); (f) the report row, and the cursor
advances only when every step completed. A run that would spend more than half the hourly
request budget is refused before any call (`BudgetExceeded`).

**Alerts.** One Sentry event at level **fatal** per run with at least one new mismatch
(`reconciliation-mismatch`, counts by kind only) and one per failed run
(`reconciliation-failed`). Both reach the owner by the existing Sentry rule. The daily
summary email waits for the Phase 7 email provider.

**The page `/reconciliation/`** (billing and above): last run with its counts, duration
and request count; recent runs; the mismatch list, unresolved first, with masked
identifiers only. **Resolve with note** (billing; a note is required and is guarded)
marks a mismatch resolved and writes an audit row. **Run now** (admin) queues a manual
run; the page shows its job state. Settings and defaults:
`PENDING_PAYMENT_MAX_BUSINESS_DAYS=5`, `RECONCILE_LOOKBACK_DAYS=35`,
`RECONCILE_OVERLAP_HOURS=48`, `RECONCILE_DEPOSIT_DAYS=14`, `RECONCILE_LOCAL_TIME=02:00`,
`RECONCILE_TIMEZONE=America/New_York`.

### 10.1 Proving the pending-payment alert on staging

The owner's done-when needs a pending payment older than the threshold. From a shell
inside the **worker** or web container (read-only on Stax; this touches only our row):

```bash
python backend/manage.py age_pending_payment --payment <payment id> --days 10
```

It moves `initiated_on` back ten days on a **pending** payment only, writes an audit row,
prints the new date, and is refused when `APP_ENV=production` or for any other payment
status. Then **Run now** on `/reconciliation/`: the run lists the payment under
`pending_too_old` and the Sentry fatal email arrives. The payment itself is unchanged
otherwise; Stax is not told anything.

### 10.2 Running a reconciliation by hand

Inside the **worker** container only (`STAX_CALLER=worker` there):
`python backend/manage.py reconcile` runs at once under the same lock and prints the
counts; exit code 1 means the run failed and the row says at which step. From any other
container the command refuses.

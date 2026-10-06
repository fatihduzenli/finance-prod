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

## 11. Setup fees, deposits, store costs and the P&L (PLAN-010b)

**Setup charges.** When an admin creates a lease, the form ends with a checklist of the active
fee types (access fee, security deposit, front sign fee, architecture fee, business purchase
fee, phone service; first-year insurance is inactive, ADR-0007). Tick the items the operator
owes, type cost and price, and save once: the lease and its charges are created together. Billing
can add a charge later from the lease page. Each charge has its own page: **Record payment**
(wire, Zelle, other, with a note; a full payment marks the linked Stax invoice paid through the
worker, as rent does), **Link invoice** (an unlinked one-off invoice of the operator, created by
staff in the Stax dashboard), **Edit** (price locked once a payment exists), **Waive** (admin,
note required). Fee types and premium costs are edited in the Django admin by an admin.

**Deposits.** A security deposit is held, not earned. When a lease ends (**End lease** on the
lease page, admin: end date and reason; autopay must be paused first), the lease page offers
**Resolve deposit** on each paid deposit: *refunded* (amount, date, method, note; a partial
amount records the rest as withheld), *withheld* (the whole amount becomes revenue that month),
or *transferred* to another lease of the same operator. The refund appears in the payments
ledger as kind `refund_deposit` and in no revenue figure.

**Store costs.** On a store's page, **Set landlord rent** (billing) records the landlord rent,
an optional other monthly cost with its description, and the landlord lease end, effective from a
date; the page then shows the margin against the operator rent of the active lease. A store with
no cost row shows "unknown", never 0. To load many stores at once, fill the owner's template
(`docs/templates/Store_Costs_Template.xlsx` or `.csv`; only the yellow columns; numbers only;
dates MM/DD/YYYY; an empty effective date means the store's open date; an empty rent means "not
filled") and run, from a shell in the web container:

```bash
python backend/manage.py import_store_costs /path/to/file.xlsx --dry-run
python backend/manage.py import_store_costs /path/to/file.xlsx
```

The dry run prints what would happen and writes nothing. The real run prints read / not filled /
unchanged / created / updated / refused, and each refused row as "row N: reason"; it exits 1 when
any row was refused and the other rows are still applied. Running the same file twice changes
nothing. A later effective date closes the previous row the day before. Example files with two
rows: `docs/templates/examples/store_costs_two_rows.{csv,xlsx}` (stores 1796 and 437). The
image does not carry `docs/`, so inside the container use the identical copy that ships with the
tests:

```bash
az containerapp exec -g finance-sandbox -n finance-staging-web --command bash
python backend/manage.py import_store_costs backend/tests/fixtures/store_costs/two_rows.xlsx --dry-run
python backend/manage.py import_store_costs backend/tests/fixtures/store_costs/two_rows.xlsx
```

(Stores 1796 and 437 must exist with those store numbers first, or both rows are refused as
"unknown store".)

**Profit and loss.** `/reports/pnl/` (every role can read it) shows, by store, operator or
month over a range of up to 36 months: rent billed and collected, fees billed and collected
(setup fees plus insurance, never deposits), landlord rent, other costs, fee costs, and the gross
margin on a billed and on a collected basis. "unknown" means an input is missing (no store cost
row, a null fee cost or premium cost); the row says which. A highlighted row is a month in which
the store costs money with no operator. **Export CSV** gives the same figures; an unknown amount
is an empty cell with a `*_known` column beside it.

### 11.1 Correcting or closing a store cost row (admin, PLAN-010c)

On the store page each live cost row has **Correct** and, for the open row, **Close**.
*Correct* opens the row's values; change the amount, the other cost, the dates or the landlord
lease end, give a note, and save: a new row replaces the old one, which stays in the history
struck through with who, when and why. Nothing is overwritten. A correction that would overlap
another live row is refused. *Close* sets an end date on the open row with a note. Months after a
closed row read **unknown** in the margin and the P&L; to state "no cost from here", add a row
with landlord rent 0.00, which is a known zero. After a close, a new row or an import must start
after the closed row's end.

The P&L now stops a query that runs longer than `REPORT_STATEMENT_TIMEOUT_MS` (15 seconds by
default) and says "The report took too long. Try a shorter range, or try again in a minute."
`import_store_costs` refreshes the planner statistics of `store_costs` after a run that changed
anything; the Step 13 import must call the same helper for every table it loads.

## 12. Documents on stores and leases (PLAN-010c, ADR-0008)

**What it is.** The Documents section on a store or lease page holds lease agreements,
amendments, floor plans, photos, notices and other files. Billing and admin upload (button or
drag and drop, one file at a time, with a kind and an optional note); every role can list,
preview (PDF, PNG, JPEG) and download; an admin deletes with a note. A file can also be attached
while creating a store or lease; it is saved after the record, and a refused file never undoes
the record.

**Rules.** pdf, docx, jpg/jpeg, png, heic; at most 25 MB; the type is read from the content and
must agree with the extension. The same file twice on one record is refused.

**No bank data, ever.** At upload the app reads the text of every PDF and DOCX, including form
field values, annotations, document properties, headers, footers, footnotes, comments and alt
text, and refuses the file with the usual sentence if it finds a card number or a routing and
account pair. Nothing is stored. A PDF that cannot be read (password-protected, damaged) is
refused, and so is a document with an embedded file, because neither can be checked. **Images and
scanned PDFs cannot be checked.** The rule for those is on every upload box: *Do not upload bank
forms, ACH authorisations, voided cheques or card details. Those stay in Stax or DocuSign.* If
one is uploaded by mistake, an admin deletes it at once (see "Purging" below).

**Where files live.** In the private container `documents` of the environment's storage
account (staging: the account named in the deployment output `documentsStorageAccountName`).
No public access, no account keys; the web app reads and writes with its managed identity. A file
never touches the container's disk or the database. Downloads and previews are streamed through
the app to a signed-in user; there is no link to share.

**Audit.** `document.uploaded`, `document.upload_refused` (reason code only),
`document.previewed`, `document.downloaded`, `document.deleted` in the audit log, with who and
which document.

**Retention and restore.**
- The database is restored by Azure Database for PostgreSQL point-in-time restore, 7 days.
- A delete in the app is soft: the row keeps `deleted_at`, the blob stays. Admins still see the row.
- The storage account keeps deleted and overwritten blobs for 30 days (soft delete, versioning)
  and a deleted container for 30 days. A database restored to any point in its 7-day window
  therefore finds every blob its rows name.
- To bring back a blob deleted in the portal within 30 days (owner, signed in):
  ```bash
  az storage blob undelete --account-name <account> --container-name documents \
    --name "<blob path from the documents row>" --auth-mode login
  ```
- The account is locally redundant (three copies in one region). Loss of the region loses the
  files; geo-redundant storage is the option, at about twice the storage price.

**Purging a file that broke the rule.** Delete it in the app (admin, with a note). The blob
then still exists for staff with storage access and in soft delete for 30 days. To remove it
for good, the owner deletes the blob and its versions in the portal and records the date in the
status log. A model does not do this.

**If uploads fail with "Document storage is not available".** Check, in order: the web app has
`DOCUMENTS_STORAGE_ACCOUNT_URL`, `DOCUMENTS_CONTAINER` and `AZURE_CLIENT_ID`; the identity has
the role *Storage Blob Data Contributor* on the container (a new assignment takes a few minutes
to take effect); the account still has shared-key access off and the container exists. "Document
storage is not configured" means the first variable is empty: the feature is off.

## 13. One-off charges, Charge now, cancel, and the audit page (PLAN-010d, ADR-0009)

**Create a one-off charge** from an operator page (lease blank or chosen) or a lease page
(billing): category (arrears, adjustment, damage, other, or a setup fee type), amount, due date,
the description that becomes the invoice line, a mandatory internal note, and how to collect it:

| Collection | What happens |
|---|---|
| Send by email | The worker creates the Stax invoice (our `INV-YYYY-NNNN` number as its reference, the description as the line) and sends it through Stax. The page shows the invoice linked and `SENT`. |
| Leave unsent (manual) | The worker creates the Stax invoice and sends nothing; record the wire, Zelle or other payment on the charge page as usual, and Stax is marked paid with the memo. |
| Autopay on the due date | The worker creates a Stax scheduled invoice with one occurrence on the due date from the operator's linked bank method; Stax generates and debits the invoice that day. Needs a linked bank method; large amounts need the confirmation box. |
| Charge now (admin) | The invoice is created, then an admin confirms the debit on a separate page. See below. |

The charge page shows the Stax status, the payments, the latest provider request (Queued, Done,
Failed with the reason), the same single Retry as elsewhere, and Cancel. A failure of the
invoice creation is never retried automatically; Retry makes one new attempt, which first looks
for an invoice Stax may already hold with our reference.

**Charge now** (admin; `STAX_PAY_INVOICE_ENABLED` must be on, which it is not on staging until
Stax enables ACH): the confirmation page repeats operator, store, amount, our invoice number and
the last four of the linked bank method, asks for the large-debit box above the threshold, and
says that one attempt is made. On confirm the worker re-reads the invoice, refuses one already
paid, and sends **one** pay request with the charge's stored idempotency ID. Success shows a
pending ACH payment that settles by webhook; a failure shows the class and field names, leaves
the charge open, and the button returns after a minute. A later attempt reuses the same ID, so
Stax cannot charge twice.

**Cancel** (billing, note required): allowed only while nothing has been paid or is pending. The
worker deletes the Stax invoice (or the unfired schedule) and, once Stax confirms, the charge
reads **Voided** and the local invoice `void`. Stax shows the invoice as **DELETED**; it has no
"voided" state for invoices. A charge with a payment is handled through refunds instead.

**Audit page** `/audit/` (admin only): filters by action (exact, or a prefix with `*` such as
`document.*`), user, record type and ID, and a date range in New York days (default the last 7
days, at most 366); 100 rows a page; **Export CSV** of the filtered rows (at most 50,000). Each
Documents section shows an **Activity** list of the last 10 document events for that record,
including refused uploads, with a link to the audit page for admins.

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

**Create a one-off charge** (replaced by the invoice builder in PLAN-012, §16: the old form
answers "gone"; what follows describes the charge each builder line becomes) from an operator
page (lease blank or chosen) or a lease page (billing): category (arrears, adjustment, damage, other, or a setup fee type), amount, due date,
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

## 14. Dashboard, month grid, arrears, reports, exports and the phone (PLAN-011)

**Home (`/`).** Signing in lands on the dashboard: total outstanding, overdue with a count, paid
this month, invoices created but not sent, **failed payment attempts** (invoices Stax marks
`ATTEMPTED`: operators who tried to pay and could not; call them first), today's run (due,
pending, returned, with links), the last reconciliation, and, while any exist, charges waived
with a live Stax invoice (see §13.1). Every figure links to the list behind it.

**Grid (`/grid/`)** is the Rents tab: stores down, months across; green paid, red unpaid, amber
pending, orange partial, grey waived, "due" before the debit, "not due" before the lease, a dot
where the charge has a note; click a cell to open the charge. Filters: operator, state, channel,
status. Export CSV gives the sheet's column order so the parallel run can be diffed.

**Arrears (`/arrears/`)**: one line per operator across stores, months behind (1, 2, 3+), total
owed including insurance and one-off charges, last payment, channels, stores; CSV.

**Reports (`/reports/`)**: A/R aging (a snapshot as of today; its grand total always equals the
dashboard's outstanding), revenue by month (billed and collected by the month the money settled;
deposits excluded), payments by method (ACH, Zelle, wire, other; no cards exist here), deposits
versus transactions (unmatched items highlighted, from the nightly reconciliation), the P&L, and
a printable **customer statement** per operator across all stores (open charges and payment
history; print from the browser; CSV). Every list page has Export CSV of the filtered rows with
headers written for an accountant (`Amount due (USD)`, `Due date`, …). Exports stop at 50,000
rows and say so in a final row.

**On a phone.** Every page fits a phone; tables turn into cards, the grid and the reports scroll
sideways with the first column fixed. To install: open the site in Safari (iPhone) or Chrome
(Android) while signed in, use "Add to Home Screen" / "Install app". The installed app caches
only static files (styles, icons, scripts); every page and every figure is fetched live, and
nothing works offline by design. Documents can be taken with the camera from the upload box.
Sign-in with the authenticator code works the same on the phone.

### 13.1 Waive, Cancel, and a live Stax invoice

**Cancel** removes the Stax invoice (it reads DELETED there) and closes the charge: nothing is
owed. **Waive** closes a charge that has no Stax invoice without collecting it. Waiving a charge
whose invoice is live at Stax is refused: cancel first. A charge already waived with a live
invoice (there was one from the night of 2026-10-05) shows on the dashboard until an admin
presses **Delete provider invoice** on its page, which removes the invoice through the worker
and records it in the audit log.

## 15. Collections e-mails, returns and NSF, attention list, contact log, calendar (PLAN-011b)

**Collections e-mails.** Each morning (09:00 New York) the app looks at overdue rent and one-off
charges and queues the next step for each: day 3 friendly reminder, day 10 formal notice, day 20
final notice (steps and templates are edited in the Django admin by an admin; merge fields
`{operator}`, `{store}`, `{amount}`, `{due_date}`, `{days_late}`, `{statement_link}`, `{company}`).
The worker sends from the company mailbox through Google. One message per charge per step, one
step per day, never to an operator marked **Handled personally** (operator page, with a note),
never without an e-mail address, never for a paid, waived, voided or pending charge. Every message
appears in the operator's and the charge's **Collection e-mails** log with its status: `sent`,
`catch_all` (sandbox), `suppressed` (mail off or rule), `failed` (class name). Billing can send a
step by hand from the charge page. **On staging every e-mail goes to the catch-all address** with
the real recipient in the subject. Mail is off until the owner stores the Google key and enables
it (§15.1).

**Returns and NSF.** A returned ACH debit sits in the returns queue and on the dashboard
("Returned debit not rescheduled: store, amount, returned date") until staff resolve it. After
calling the operator, choose **Reschedule**, enter the agreed date and a note: the app creates a
re-debit charge for the original amount and an NSF fee charge (100.00 on the operator's first
return, 50.00 afterwards; an admin can waive the fee with a note), one Stax invoice or schedule
with the two lines, and the attention item disappears. The re-debit date shows on the calendar.
When the re-debit settles, the original month reads paid again. The fee is never created by the
webhook, so a bank-side error can be resolved with a plain note and no fee.

**Attention list** (dashboard): overdue charges, returned debits not rescheduled, promises to pay
past their date, insurance due in 30 days or a missing premium, leases ending in 60 days, landlord
leases ending in 90 days, stores without an open date, operators without an e-mail, stores vacant
over 30 days, schedules flagged after a rent change, dead-letter webhooks, reconciliation
mismatches, and today's calendar entries. Each links to its page; the operator page shows that
operator's items.

**Contact log** (operator page, billing): calls, e-mails, visits and promises to pay (date and
amount). Since PLAN-012 (§16) a promise closes by itself when a matching payment settles, entries
name the store, the author may edit for 15 minutes, and an admin may delete with a reason. CSV
export.

**Calendar** (`/calendar/`): notes and reminders per store, operator, lease or general, with an
optional e-mail to you that morning; generated entries for debit days, re-debit dates, insurance
anniversaries and lease ends. **Google Calendar:** on `/settings/`, create your feed address, copy
it once, and add it in Google Calendar as "From URL". The address is a secret: anyone holding it
sees the shared staff calendar; revoke it on the same page if it leaks. No amounts or bank data
appear in the feed.

### 15.1 Enabling mail (owner and Workspace admin)

Follow plan §9 of `docs/qa/PLAN-011b-collections-nsf-attention-calendar.md`: Cloud project and
service account, Gmail API, domain-wide delegation with `gmail.send`, the key into Key Vault as
`gmail-service-account-key`, DKIM published. Then the deployment is run with `mailEnabled=true`,
`gmailSender=<the mailbox>` and `mailCatchAll=<staff address>` (staging). The first e-mail on
staging arrives at the catch-all.

### 15.2 Time zone

The app keeps New York days: charge periods, debit days, "today", the nightly job, report months.
Storage stays UTC. `tz_audit` lists the few rows whose stored date was derived under UTC and would
differ; `tz_apply --audited N` realigns exactly those rows with an audit entry each, and only on
the owner's word.

## 16. Contact log maturity, the invoice builder, disputes (PLAN-012, ADR-0011)

**Contact log.** An entry can be changed by its author for **15 minutes** after it was recorded
(the row shows "editable until"); after that, and for anyone else, use **Add correction**: the
correction is appended under the entry with its author and time, and the original stands. An
**admin** may delete an entry with a mandatory reason: the entry stays, struck through, with the
reason, for billing and admin; viewers do not see it and it is absent from their CSV. A deleted
promise counts for nothing. Every entry names the **store** when the operator has more than one
active lease (preselected with one); the store page has its own contact section and **Add entry**
button, offered whenever the store has a lease with an operator who is not archived (the current
lease, else the most recent one; PLAN-012c): the form opens with that lease fixed and the
operator set, and its title says "lease ended …" or "lease pending" when the lease is not
current. **Promises** close by themselves: when a settled, non-refund payment of at least the
promised amount settles on that lease (or on the operator, for an entry without a store) between
the day the promise was recorded and the promised date, the promise reads **Kept** with the payment
linked. Recording a new entry while a promise is open asks what happened to the **previous
promise**: **Still open (no change)** by default, or Kept, Rescheduled (a new promise with a new
date and amount) or Broken (PLAN-012c-b). Every open promise row (operator page, store page,
attention list) also carries **Kept / Reschedule / Broken** buttons: each opens a small form
(date, note; Kept and Broken ask how you learned it; Reschedule asks the new date and amount,
pre-filled) and records the entry exactly as the previous-promise path does. **One open promise
per store:** saving a new promise closes the open one on that store (or on the operator, with
no store) as Rescheduled; the form warns beforehand and the message says so, so the attention
list never shows two promises for one store. The entry that closed a promise says so in its Promise
column ("Closed $2000.00 by 2026-10-01 as Broken", linked to the promise) on both pages and in
the CSV column **Closes promise**. The log can
be filtered by status (open, kept, rescheduled, broken). A past, open promise is a broken promise
on the attention list and the dashboard whatever the balance (PLAN-011b-a).

**Invoice builder** (replaces "Create one-off charge"; billing). From a store page or an operator
page choose **New invoice**: pick the lease (or none), add lines, an
optional memo and due date. Each line has an **Item** box (PLAN-012b): start typing and pick a
catalogue item from the list, or type a new name; a **Description** is an optional detail under
the item; then quantity and unit amount (the line total is quantity times unit amount to the
cent). The item name is the Stax line text and the charge's description; the detail is the Stax
line detail. A typed item shows a **Save as catalogue item** tick: when ticked, saving or sending
the draft adds it to the catalogue (its later charges are category *setup*, like every catalogue
item; a typed item without the tick is *other*). A name held by an inactive catalogue item cannot
be saved over; an admin changes it in the catalogue. **Rent** is a line kind of its own
(PLAN-012b-b): choose Rent, the lease (one of the operator's leases, a lease not yet started is
allowed) and the month; the amount defaults to the lease rent for that month, prorated for a
partial first month, and can be edited; one rent line per lease and month, none for a month that
is already paid, invoiced or on an autopay schedule, none on a twice-monthly lease. Sending
creates that month's rent charge (or takes the existing unpaid one), links the invoice to it, so
the rent run and the catch-up never bill the month again; the grid cell shows the invoice
number and, once paid, "paid" in that month. Deposit, setup and rent lines can share one invoice
(the move-in invoice); a payment pays the lines in order and the rent part counts as rent. The
due date defaults to today + 15 (New York; never in the past). **Save** keeps a
draft (named `DRAFT-` and eight characters of its id); the draft page offers **Preview** (what the operator sees), **Print**
(a clean page for the browser's print dialog), **Edit**, **Send**, **Charge now** (admin) and
**Cancel**. **Send** takes the next `INV-YYYY-NNNN` number, makes **one one-off charge per line**
(category from the fee type: setup fee, arrears, adjustment, damage, other) and **one Stax invoice
with all the lines**, which the worker creates and sends by e-mail; the charges show the invoice
and each other ("billed with"). Every line's page shows the invoice and its provider status,
offers **Record a payment**, and refuses Waive while the Stax invoice is live (PLAN-012c-a). A
provider payment on the invoice is allocated to the lines in order; a wire, Zelle or other payment
is recorded on the line it pays. When the lines' settled payments together cover the invoice, the
Stax invoice is marked paid as cash or check with the memo and the "partial amount" flag clears
on every line; until then the paying line carries the flag. The statement, the ledger, the P&L
and deposits held read the lines, not the payment's first charge. The read-only command
`invoice_line_links --check` lists any builder invoice whose lines and charges disagree. **Charge now** debits the invoice total from the
operator's linked bank method after the 10d confirmation page (large amounts need the typed
confirmation; `STAX_PAY_INVOICE_ENABLED` must be on). **Cancel** on a draft deletes nothing in
Stax (nothing was sent); cancelling a sent invoice is the charge page's cancel as before. Staff
sending a draft twice get the first invoice back; the old charge form's addresses answer "gone".

**Disputes** (`/disputes/`, Payments menu; billing and above). The disputes Stax reports for the
last 90 days, ordered by **respond-by** date with the days left (highlighted under 7 days; "Past"
once over), the provider's status and reason **verbatim**, the amount, the operator, store and
payment when the disputed transaction is ours, and the transaction masked. The list is read by the
worker: each day with the collections tick, on every dispute webhook, and when billing presses
**Refresh** (one read queued at a time; the page shows "Last synced"). Stax is the source of truth
for disputes: nothing here accepts, contests or closes a dispute, and nothing changes the payment
(the webhook flags the charge for review as before). **Evidence** (admin): under a dispute, give
the file a name, choose a PDF, JPEG, PNG or TIFF smaller than 10 MB, and for an image tick the
notice box; the file is stored as a document of the **operator** (kind *dispute evidence*), the
10c checks run on PDFs (a bank form is refused and nothing is kept), and the worker sends it to
Stax. The row shows **Queued / Sent / Failed** with the provider's sentence when Stax refuses it
(after the respond-by date, for example) and **Send again** for a failed row. No evidence can be
added once the respond-by time has passed. A dispute not linked to one of our operators takes no
evidence (there is nowhere to keep the file). The full dispute workflow (accept, outcomes,
reminders before the deadline) is BACKLOG §1 row 4, after Step 17.

**Operator consent (PLAN-012a).** The operator form (billing) has an **SMS phone** in
international format (`+1` and the number, digits only) and two dates, **SMS consent given on**
and **E-mail consent given on**, shown on the operator page and in the operator CSV. They are
records only: nothing sends an SMS, and the collections e-mails do not read them yet. The
invoice tables also carry a line kind and separate fee and surcharge amounts, always zero today;
nothing on any page changes.

**Feature freeze.** Nothing in `docs/BACKLOG.md` §1 starts before Step 17 (Checkpoint 12).

## 17. Lease end, operator transfer, installment plans, proration (PLAN-012e, ADR-0012)

**Proration.** A lease that starts mid-month is charged for the days used: rent × days used ÷
days in the month, rounded once to the cent, start and end days both counted; the first rent
charge shows the arithmetic ("13–31 Oct, 19 of 31 days of $2,000.00 = $1,225.81") on the charge
page, the grid cell and the statement. "Prorate first month" on the lease form (default on) turns
it off for a lease that pays a full first month. Months after a lease's end date are never
charged. **The rent run catches up** (PLAN-012b-b): the daily job and the "Create today's
charges" button both create today's debits and then every missing debit between each lease's
start date and today (at most 24 months back), the first month prorated, each due on its debit
date or on the start date when the start falls after that month's debit day; the page reports
"Created (catch-up): M". A lease saved with a start date on or before today gets its first
charge at once; a lease starting later gets it on the start date.

**End lease** (store page, admin). Four steps, which can be left and resumed; Cancel deletes the
draft and changes nothing. (1) **Terms:** the end date (today by default, never in the future),
the reason (moved out, terminated, transfer, other) with a few words, and "Prorate last month"
(default on). (2) **Blockers:** every charge with a live Stax invoice or a provider schedule, every
rent or insurance schedule that is active or paused at Stax, every pending provider payment, a
live installment plan, and a security deposit not collected ("collect it, or waive or cancel the
charge"), each with a link to where it is cancelled, collected or stopped; **Stop autopay** (lease page, admin)
deletes the schedules at Stax through the worker so the blocker clears. The wizard cannot pass
while anything is listed and checks again at confirm. (3) **Statement:** opening it creates the
final month's prorated rent charge (and any missing earlier month) so it appears as a real line
with its link, offset against the deposit like any other balance; the last month's rent is
reduced to the days used (an unpaid charge is reduced; a paid one gives the excess back as a
credit), the deposit line reads "Deposit charged $X, collected $Y" when they differ, rent charged for months after the end date waived (or credited
when paid), every open balance on the lease, the deposit held, and the **deductions** you type
(damage, cleaning, unpaid balance offset, other); refund due = deposit − deductions, or the
amount owed. (4) **Settlement:** how the refund is paid (Zelle, wire or check; never through
Stax), the date and a reference; an unpaid-balance offset pays the oldest open charges from the
deposit; what the deposit does not cover becomes one charge, "Lease-end balance", collected by
hand. **Confirm** lists every effect and needs the box ticked. Then the lease is ended, the store
shows "Vacant since <date>", the worker stores the final statement as a PDF under the store's
documents (kind *Final statement*) and queues the e-mail to the operator (sent when mail is on,
to the catch-all on staging, recorded as suppressed when mail is off). Every step is audited.
An installment plan that is live on a charge of the lease is a blocker too: let it complete or
cancel it (admin) first. A lease end that never reached the statement step is not "in
progress": the store shows no banner and a transfer simply takes the draft over; one that
reached the statement shows "A lease end is in progress (statement step)" with **Continue** and
**Cancel** (admin). Drafts whose terms were never set are removed after seven days by the daily
job.

**Transfer** (store page, admin). Pick the incoming operator from the list or create one on the
spot (name, e-mail, phone, SMS phone and consents); the transfer cannot pass while the outgoing
operator owes anything or any lease-end blocker stands (the same list, with links); then the
end-of-lease steps run for the outgoing lease (reason *transfer*); then the incoming lease is
entered (start date no earlier than the day after the end, rent, deposit, setup charges from
the catalogue), and confirm ends the outgoing lease, creates the incoming one and leaves the
deposit and setup charges as one invoice-builder draft ("Move-in charges for store <label>")
for staff to review and send. Nothing reaches Stax until that draft is sent. No store ever has
two leases in effect on one day. **If the statement would leave a balance** on the outgoing
operator (deductions above the deposit), the transfer is blocked: collect it or change the
deductions, or an admin writes a reason on the confirm page ("Allow the balance to remain"); the
override is audited, the "Lease-end balance" charge stays on the outgoing operator and shows on
the attention list ("Lease-end balance: …, transfer, allowed by …") until it is paid.

**Installment plans** (billing). On a rent charge page or a one-off charge page, **Split into
installments** divides what is still owed into 2 to 12 equal installments (the rounding cent on
the last), from a first date (today or later) weekly, every two weeks or monthly (the 31st
becomes the month's last day); the preview can be edited line by line before confirming as long
as the amounts still add up and the dates stay in order. Each installment is a charge of its own
("Installment 2 of 3: Rent 2026-10"), collected the way the parent is: autopay on the due date
(one Stax schedule each; needs the linked bank method), by e-mail (one Stax invoice each) or by
hand (nothing at Stax). While the plan runs the parent is paid through its installments only: it
leaves the open balances, aging, arrears and the attention list, its page and grid cell show
"plan n of N paid, $X of $Y", and a payment typed on it is refused. When every installment is
paid the plan is completed and the parent is marked paid with an audit row and no payment of its
own. A one-off charge whose own Stax invoice (one charge on it) or schedule is live can be
split: the split page says so, and on confirm the worker **releases** that invoice or schedule
(deletes it at Stax; the charge stays open here, never voided) and only then creates the
installments' own invoices or schedules; the plan page says "Waiting for the payment provider to
release…" until then, and a failed release shows on the charge page with Retry. If the operator
pays the old invoice before it is released (or a late payment arrives after), the payment lands
on the charge, the plan goes on the attention list ("Payment on a split charge: …") and staff
cancel or reschedule the plan. While a plan runs, its charge cannot be cancelled or waived on
its own: cancel the plan. A line of a multi-line invoice cannot be split while that invoice is
live; a rent charge's invoice is cancelled first (that never voids the rent charge), and rent
autopay must be stopped or paused first. Deposits, re-debits, NSF fees and installments are
never split (PLAN-012b-a). A
**returned installment** debit follows the NSF rule from the returns queue (re-debit plus the
fee, $100 the first time and $50 after, agreed by phone) and puts the plan on the attention list
("Installment returned: …") until the return is resolved. **Reschedule** (billing, plan page)
moves the installments not yet paid to new dates from a new first date; Stax schedules and
invoices follow through the worker. **Cancel** (admin, a note) cancels the unpaid installments
(their Stax objects are deleted by the worker) and leaves the parent open for what they did not
collect. The P&L and the payments ledger count the money once, under the parent, each payment
in the month it settled; installments are never billed lines; the ledger's CSV names the parent
in "Installment of".

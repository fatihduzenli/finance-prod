# User-facing copy inventory: Django app at `/Users/fatihduzenli/Downloads/finance project /backend`

Nothing was changed. All paths below are relative to that directory (the folder name ends in a space). Sections 3, 5 and 7 were pulled out with regex and AST scripts. A button or link whose text runs across several lines could be missing, but I checked and found none.

**Main gaps:**
- **Section 5 is not the full list.** There are 638 module-level sentence constants in `ledger/services/*.py` and `ledger/views/*.py`. Only the 123 that contain a technical term are listed, plus SPLIT_HELP and a few the brief named. The rest are plain sentences, counted per file at the end of section 5.
- **No thousands separator anywhere in settings.** I found no `USE_THOUSAND_SEPARATOR`, `NUMBER_GROUPING` or format-module setting in `finance/`. So every plain `${{ x }}` in a template shows `2000.00`, not `2,000.00`. Only `|floatformat:"2g"` and Python `:,` group the digits.

---

## 1. Main navigation and menus (`finance/templates/finance/base.html`, `finance/templates/finance/banner.html`)

| Where | Current text | Technical term? |
|---|---|---|
| Mobile menu toggle | Menu | N |
| Nav link | Grid | N |
| Nav link | Dashboard | N |
| Nav link | Arrears | N |
| Nav link | Calendar | N |
| Nav link | Stores | N |
| Nav link | Operators | N |
| Nav link | Leases | N |
| Nav link | Payments | N |
| Nav link | Disputes | N |
| Nav link | Reports | N |
| Nav link | Profit and loss | N |
| Nav link | Returns | N |
| Nav link | Webhooks | Y (webhook) |
| Nav link | Reconciliation | Y (reconciliation) |
| Admin menu heading | Admin | N |
| Admin menu item | Django admin | Y (names the framework) |
| Admin menu item (billing role only) | Rent run | N |
| Admin menu item (admins only) | Audit | N |
| Search placeholder | Store, city or operator | N |
| Search label (for screen readers) | Search stores and operators | N |
| Nav link | Settings | N |
| Button | Sign out | N |
| Browser tab title suffix | `<page> \| Finance` | N |
| Sandbox banner (`banner.html`) | SANDBOX — NO REAL MONEY | Y (all caps) |
| Sign-in app name (`finance/settings.py:242`) | Finance (SANDBOX) | Y (all caps) |

## 2. Page titles (`<title>` / `<h1>`)

Paths are under `ledger/templates/ledger/` unless shown in full. `{…}` is a value filled in on the page. Only the `<h1>` is shown where it differs from the title.

| Template | Title | H1 (if different) | Technical term? |
|---|---|---|---|
| finance/templates/finance/settings.html | Settings | | N |
| arrears/page.html | Arrears | | N |
| audit/page.html | Audit log | | N |
| calendar/page.html | Calendar {YYYY-MM} | Calendar: {Month YYYY} | N |
| calendar/detail.html | {entry title} | | N |
| calendar/form.html | Edit {title} / New calendar entry | Edit calendar entry / New calendar entry | N |
| charges/detail.html | Charge {store} {YYYY-MM} | Rent {YYYY-MM}, debit {n} [Needs review] | Y (needs review) |
| charges/link_invoice.html, one_off_charges/link_invoice.html | Link an invoice | | N |
| charges/payment_form.html, one_off_charges/payment_form.html | Record a payment | | N |
| contacts/correct.html | Add a correction | | N |
| contacts/delete.html | Delete contact entry | | N |
| contacts/edit.html | Edit contact entry | | N |
| contacts/outcome.html | {action}: promise of {operator} | | N |
| dashboard/home.html | Dashboard | | N |
| deposits/resolve.html | Resolve deposit {store} | Resolve {fee type} | N |
| disputes/page.html | Disputes | | N |
| documents/unavailable.html | Document unavailable | | N |
| grid/page.html | Grid | | N |
| installments/split.html | Split {charge} into installments | | N |
| installments/plan.html | Installment plan, {charge} | + {plan status} | N |
| installments/reschedule.html | Reschedule the installments of {charge} | | N |
| installments/cancel.html | Cancel the installment plan of {charge} | | N |
| invoices/new.html | {heading} | | N |
| invoices/draft.html | {number} | Invoice {no} / Invoice draft {no} + {status} | N |
| invoices/preview.html | Preview {number} | (none) | N |
| invoices/print.html, invoices/_invoice.html | Invoice {number} | | N |
| invoices/cancel.html | Cancel {number} | Cancel invoice {no} / Cancel {draft no} | N |
| invoices/charge_now.html, one_off_charges/charge_now.html | Charge now {number} | Charge now | N |
| lease_ends/start.html | End lease {store} | | N |
| lease_ends/terms.html | End lease {store}: end date | (H1 from _steps: End lease {store}) | N |
| lease_ends/blockers.html | End lease {store}: live items | same | N |
| lease_ends/settlement.html | End lease {store}: settlement | same | N |
| lease_ends/statement.html | End lease {store}: statement | same | N |
| lease_ends/confirm.html | End lease {store}: confirm | same | N |
| lease_ends/confirmed.html | Lease end {store} | | N |
| leases/list.html | Leases | | N |
| leases/detail.html | Lease {store} | + [Needs review] | Y (needs review) |
| leases/form.html | Edit lease {store} / New lease | | N |
| leases/enrol.html | Autopay for {store} | Enable autopay | N |
| leases/stop_autopay.html | Stop autopay: lease {store} | Stop autopay | N |
| one_off_charges/detail.html | {charge label} {store} | + [Voided] [Deposit] [Legacy] [Needs review] | Y (legacy, needs review) |
| one_off_charges/form.html | Edit {label} / Add setup charge | | N |
| one_off_charges/gone.html | Create one-off charge | | N |
| one_off_charges/cancel.html | Cancel {label} | | N |
| one_off_charges/waive.html | Waive {label} | | N |
| operators/list.html | Operators | | N |
| operators/detail.html | {operator} | + [Archived YYYY-MM-DD] | N |
| operators/form.html | Edit {operator} / New operator | | N |
| operators/link.html | Link {operator} | Link payment provider IDs | Y (provider, IDs) |
| operators/contacts.html | Contact log: {name} | | N |
| operators/contact_form.html | Add entry: {name} | | N |
| payments/list.html | Payments | | N |
| payments/detail.html | Payment | Payment/Refund of ${amount} | N (amount has no separator, see §6) |
| payments/refund.html | Refund a payment | | N |
| payments/void.html | Void a payment | | N |
| reconciliation/page.html | Reconciliation | | Y (reconciliation) |
| reports/index.html | Reports | | N |
| reports/aging.html | A/R aging | | Y (A/R jargon) |
| reports/deposits.html | Deposits versus transactions | | N |
| reports/payments_by_method.html | Payments by method | | N |
| reports/pnl.html | Profit and loss | | N |
| reports/revenue.html | Revenue by month | | N |
| reports/statement.html, statement_public.html | Statement: {operator} | | N |
| returns/list.html | Returned payments | | N |
| run/today.html | Rent run {YYYY-MM-DD} | | N |
| search/results.html | Search | | N |
| stores/list.html | Stores | | N |
| stores/detail.html | Store {store} | + [Needs review] | Y (needs review) |
| stores/form.html | Edit store {store} / New store | | N |
| stores/charge_now_pick.html | Charge now: store {store} | | N |
| stores/cost_form.html | Set landlord rent {store} | Set landlord rent | N |
| stores/cost_correct.html | Correct cost row {store} | Correct a cost row | N |
| stores/record_payment.html | Record a payment: store {store} | Record a payment | N |
| stores/schedule_payment.html | Schedule a payment: store {store} | Schedule a payment | Y (schedule) |
| transfers/start.html | Transfer store {store} | | N |
| transfers/blockers.html | Transfer store {store}: live items and balances | | N |
| transfers/operator.html | Transfer store {store}: incoming operator | | N |
| transfers/incoming.html | Transfer store {store}: incoming lease | | N |
| transfers/confirm.html | Transfer store {store}: confirm | | N |
| transfers/confirmed.html | Transfer of store {store} | | N |
| webhooks/list.html | Webhook events | | Y (webhook) |
| webhooks/detail.html | Webhook event {type} | Webhook event: {type} | Y (webhook; the type is a raw provider code) |

Store page tabs (`ledger/services/workspace.py:101`): Charges, Invoices, Payments, Schedules (Y: schedule), Documents, Contact log, History. Filters: Open / Paid / All, Unpaid / All.

## 3. Buttons and action links (one example template each, deduplicated)

| Text | Example template | Technical term? |
|---|---|---|
| Add correction | contacts/correct.html | N |
| Add entry | operators/contacts.html | N |
| Add line | invoices/_draft_form.html | N |
| Add note or reminder | calendar/page.html | N |
| All entries | operators/detail.html | N |
| Allow the balance to remain | transfers/confirm.html | N |
| Archive | operators/detail.html | N |
| Broken / Kept (promise outcome links) | contacts/_outcome_links.html | N |
| Calendar feed | calendar/page.html | N |
| Cancel | stores/detail.html | N |
| Cancel charge | one_off_charges/cancel.html | N |
| Cancel draft / Cancel invoice | invoices/cancel.html | N |
| Cancel plan / Cancel the plan | installments/plan.html / cancel.html | N |
| Cancel the lease end / Cancel the transfer | lease_ends/_cancel.html | N |
| Charge ${amount} now / Charge ${total} now | one_off_charges/charge_now.html / invoices/charge_now.html | N (no separator) |
| Charge now | invoices/draft.html | N |
| Choose / Save and choose | transfers/operator.html | N |
| Close | stores/detail.html | N |
| Continue / Start | transfers/start.html, blockers.html | N |
| Continue the lease end | transfers/start.html | N |
| Correct | stores/detail.html | N |
| Create a calendar address | finance/settings.html | N |
| Create the plan | installments/split.html | N |
| Create today's charges | run/today.html | N |
| Delete | calendar/detail.html | N |
| Delete provider invoice | one_off_charges/detail.html | Y (provider) |
| Download | documents/_section.html | N |
| Edit | operators/_contacts_table.html | N |
| Enable autopay | leases/enrol.html | N |
| End lease / End the lease | leases/detail.html / lease_ends/confirm.html | N |
| Export CSV / Export rent charges CSV / Export one-off charges CSV | reconciliation/page.html / stores/_tab_charges.html | Y (CSV) |
| Filter | payments/list.html | N |
| Hide the rows / Show the rows | reports/pnl.html | N |
| Link | operators/link.html | N |
| Link invoice | charges/link_invoice.html | N |
| Link provider IDs | operators/detail.html | Y (provider, IDs) |
| Match invoices | run/today.html | N |
| Move the unpaid installments | installments/reschedule.html | N |
| New invoice / New lease / New operator / New store | operators/detail.html etc. | N |
| Newer / Older / Previous / Next / ← Previous / Next → / This month | audit, reconciliation, calendar | N |
| Open in the audit log | stores/_tab_history.html | N |
| Open in the payments ledger | stores/_tab_payments.html | N |
| Pause autopay / Resume autopay / Stop autopay | leases/detail.html | N |
| Preview / Print / Print view | installments/split.html, reports/statement.html, invoices/draft.html | N |
| Record a payment / Record payment | charges/detail.html / charges/payment_form.html | N |
| Refresh | disputes/page.html | N |
| Refresh methods | leases/enrol.html | N |
| Refund / Void | payments/detail.html | N |
| Refund at the payment provider | payments/refund.html | Y (provider) |
| Void at the payment provider | payments/void.html | Y (provider) |
| Replace the address / Revoke | finance/settings.html | N |
| Replay | webhooks/detail.html | Y (replay of a webhook) |
| Reschedule | returns/list.html | N |
| Resolve deposit | deposits/resolve.html | N |
| Resolve with a note / Resolve with note | returns/list.html / reconciliation/page.html | N (two wordings) |
| Retry | charges/detail.html | N |
| Retry marking the invoice paid | charges/detail.html | Y (mark) |
| Retry sync | operators/detail.html | Y (sync) |
| Revoke every statement link sent | operators/detail.html | N |
| Run now | reconciliation/page.html | N |
| Save / Save and continue / Save correction / Save draft | calendar/form.html etc. | N |
| Schedule payment | stores/schedule_payment.html | Y (schedule) |
| Search | operators/list.html | N |
| Send / Send again / Send by e-mail / Send ${total} by e-mail | stores/_tab_invoices.html, disputes/page.html, collections/_charge.html, invoices/draft.html | N |
| Set landlord rent | stores/detail.html | N |
| Show | calendar/page.html | N |
| Split into installments | invoices/draft.html | N |
| Statement / Store page | operators/detail.html | N |
| Store and send | disputes/page.html | N |
| Transfer the store | transfers/confirm.html | N |
| Upload | documents/_section.html | N |
| Waive | one_off_charges/waive.html | N |

Store/operator action bar (`ledger/services/workspace.py:409-512`, shown in stores/detail and operators/detail): Create invoice, Record a payment, Charge now, Add setup charge, Schedule a payment (Y: schedule), Create missing charges, Upload document, Add contact entry, New lease, Edit lease, Transfer to another operator, End lease.

## 4. Status labels

### `ledger/models.py` TextChoices (label as shown)

| Choice set (line) | Labels | Technical term? |
|---|---|---|
| RentChargeStatus (538) | Due, Pending, Paid, Partially paid, Unpaid, Waived | N |
| OneOffChargeStatus (846) | Owed, Partially paid, Paid, Waived, Voided | N |
| InvoiceStatus (1317) | Draft, Ready, Sent, Closed, Void | N |
| InvoiceDraftStatus (1494) | Draft, Sent, Cancelled | N |
| PaymentStatus (1682) | Pending, Settled, Returned, Voided | N |
| CollectionStatus (379) | Active, Paused, Prospective, Ended | N |
| StoreStatus (336) | Prospective, Active, Closed, Terminated | N |
| ScheduleState (494) | Not created, Active, Paused, Failed, Ended | Y (schedule as a state) |
| ScheduleKind (489) | Rent, Insurance | N |
| InstallmentPlanStatus (1105) | Active, Needs attention, Completed, Cancelled | N |
| InstallmentCadence (1123) | Weekly, Every two weeks, Monthly | N |
| InstallmentCollection (1129) | Autopay on the due date, Send by email, Manual payment | N ("email" here, "e-mail" elsewhere) |
| LeaseEndStatus (2661) / LeaseTransferStatus (2770) | Draft, Confirmed | N |
| LeaseEndReason (2654) | Moved out, Terminated, Transfer to another operator, Other | N |
| ContactStatus (2444) | Open, Kept, Rescheduled, Broken | N |
| ContactKind (2429) | Call, E-mail, Visit, Promise to pay | N |
| DepositResolutionKind (1894) | Refunded, Withheld, Transferred | N |
| ResolutionKind (1689) | Resolved with a note, Rescheduled | N |
| DeductionKind (2744) | Damage, Cleaning, Unpaid balance offset, Other | N |
| RefundMethod (2666) | Zelle, Wire, Check | N |
| PaymentMethod (1662) | ACH, Zelle, Wire, Other, Credit | Y (ACH) |
| MoneyMethod (1672) | ACH, Zelle, Wire, Other | Y (ACH) |
| PaymentKind (1643) | Rent, Insurance, Adjustment, One-off charge, Deposit refund, Credit refund | N |
| PaymentChannel (393) | ACH debit through the payment provider, Wire, Zelle, Unknown, Other | Y (provider, ACH; stored value is "stax") |
| CollectionMethod (873) | None, Autopay on the due date, Send by email, Manual payment, Charge now | N |
| OneOffCategory (857) | Setup, Arrears, Adjustment, Damage, Other, Re-debit, NSF fee, Installment | Y (NSF) |
| DocumentKind (1977) | Lease agreement, Amendment, Floor plan, Photo, Notice, Other, Dispute evidence, Final statement | N |
| DocumentSource (1971) | Upload, DocuSign | N |
| DocumentContentType (1992) | PDF, Word, JPEG image, PNG image, HEIC image, TIFF image | Y (file-type acronyms) |
| EnrolmentStatus (191) | Not enrolled, Enrolled | N |
| SyncStatus (196) | Not applicable, Awaiting link, Pending, Linked, Failed | Y (sync) |
| SyncFailure (206) | Provider error, Refused before sending, Not permitted from this process, Feature disabled, Calls paused after repeated failures, Failure budget exhausted, Rate limit wait exceeded, Rejected by the provider, Provider unavailable, Provider unreachable, Wrong merchant, Linked to another operator | Y (provider, process, rate limit, failure budget) |
| ReviewReason (386) | Pays by wire, Dormant, Closed, Unknown | N |
| RentSource (455) | Sheet, current rent; Sheet, inferred; Staff | Y (sheet = import) |
| LegacyStatus (624) | Paid, Unpaid | Y (legacy, in the class name) |
| AdjustmentReason (696) | Cross-store balance, Credit, Discount, Carry forward | N |
| StoreBalanceKind (755) | Insurance opening balance, Sign, Deposit or access | N |
| InvoiceLineKind (1424) | Charge, Fee, Surcharge, Tax, Discount | N |
| StoreCostSource (1228) | Import, Staff | N |
| DisputeEvidenceStatus (2163) | Queued, Sent, Failed | Y (queue) |
| MessageStatus (2290) | Queued, Sent, Failed, Suppressed, Sent to the catch-all address | Y (queue, catch-all) |
| MessageKind (2309) | Collections ladder, Final statement | Y (ladder) |
| ChargeEntityType (2300) | Rent charge, One-off charge, Lease end | N |
| CalendarEntryKind (2569) | Note, Reminder | N |
| NoteEntityType (777) | Store, Operator, Lease | N |

### `payments/models.py` labels shown to staff (job banner, webhooks, reconciliation)

| Choice set | Labels | Technical term? |
|---|---|---|
| WebhookOutcome (123) | Received, Processed, Ignored, Failed, Dead letter | Y (webhook, dead letter) |
| WebhookObjectIdSource (110) | None, id, mergeVars.invoice.id, invoice.id, transaction.id, data.id, batch_id | Y (raw field names) |
| JobKind (191), shown in `_job_status.html` | Operator sync, Operator sync retry, Operator link, Enable/Pause/Resume/Stop autopay, Mark the provider invoice paid, Retry marking the provider invoice, Refund, Void, Refresh payment methods, Match invoices, Replay webhook event, Run reconciliation, Create the provider invoice, Send the provider invoice by email, Delete the provider invoice, Create the provider schedule, Delete the provider schedule, Charge the provider invoice now, Send a collection e-mail, Send a morning e-mail, Read the disputes, Send dispute evidence, Make the final statement and its e-mail | Y (sync, provider, mark, schedule, webhook, reconciliation) |
| JobStatus (269) | Queued, Running, Done, Failed, Dead | Y (queue, dead) |
| ReconciliationTrigger / Status (326/333) | Scheduled, Run now / Running, Done, Failed, Skipped | Y (scheduled) |
| ReconciliationStep (341) | None, Invoices, Transactions, Deposits, ACH rejections, Pending payments, Report | Y (ACH) |
| MismatchKind (353) | Invoice status differed, Payment restored, Deposit row unmatched, Settled payment not deposited, Return missed by webhook, Pending too long | Y (webhook) |

### Grid cell words (`ledger/services/grid.py`)

| Where | Current text | Technical term? |
|---|---|---|
| Status filter (117-123) | Paid, Unpaid, Pending, Partially paid, Due, Waived, Not due | N |
| Cell text (286-299) | paid {amt}; unpaid {amt}; pending {amt} (from YYYY-MM-DD); partial {amt} of {amt}; due {amt}; waived; not due | N (lower case; no "$") |
| Screen-reader label (265) | {YYYY-MM}: no charge[, has a note] | N |
| Filter messages (129-131) | Unknown operator; showing every operator. / Unknown channel; showing every channel. / Unknown status; showing every status. | N |
| CSV header (127) | Store number, Store, State, Operator, Channel | N |
| workspace.py:131-135 | Not set up; Rent debit; Re-debit of a returned payment; Carried by re-debit; Needs premium (631) | N |

## 5. Help texts and fixed sentences

### 5a. Constants with a technical term (`ledger/services/` unless the path starts with `views/`)

Placeholders like `{X_MAX_LENGTH}` in f-strings are filled with numbers before display, so they are not flagged. The calendar, collections, disputes, invoice_builder, lease_end and store_costs constants of that kind were left out.

| File:line | Constant | Current text | Term(s) |
|---|---|---|---|
| installments.py | SPLIT_HELP | Split what is still owed on this charge into equal installments, the last one carrying the rounding cent. Each installment is a charge of its own, collected the way this charge is; while the plan runs, this charge is paid through them. | N |
| installments.py:253 | PLAN_HELP | Each installment is collected on its own date. A returned installment puts the plan on the attention list until it is resolved from the returns queue. Reschedule moves the installments not yet paid; Cancel (admin) stops the plan and leaves the charge open for what was not paid. | queue |
| installments.py:259 | RESCHEDULE_HELP | Only installments with no payment move; paid ones keep their dates. Autopay schedules and e-mailed invoices at the payment provider are updated by the worker. | schedules, provider, worker |
| installments.py:263 | CANCEL_HELP | The unpaid installments are cancelled (their invoices or schedules are deleted at the payment provider by the worker) and the charge becomes one open charge for what they did not collect. | schedules, provider, worker |
| installments.py:164 | LIVE_SCHEDULE | A payment provider schedule will collect this charge: collect it or cancel the charge first. | provider, schedule |
| installments.py:171 | SCHEDULE_FIRED | The schedule already generated its invoice; collect or cancel it first. | schedule |
| installments.py:173 | RELEASE_NOTICE_INVOICE | Invoice {number} at the payment provider will be deleted (the charge stays open here) and each installment is collected on its own. | provider |
| installments.py:177 | RELEASE_NOTICE_SCHEDULE | The payment provider schedule of this charge will be deleted (the charge stays open here) and each installment is collected on its own. | provider, schedule |
| installments.py:181 | RELEASE_NOTICE_AUTOPAY | Rent autopay of this lease will skip this month's debit: its payment provider schedule is deleted and created again from next month's debit (the charge stays open here), and each installment is collected on its own. | provider, schedule |
| installments.py:187 | RELEASE_PENDING_INVOICE | Waiting for the payment provider to release invoice {number}. | provider |
| installments.py:188 | RELEASE_PENDING_SCHEDULE | Waiting for the payment provider to release the schedule of this charge. | provider, schedule |
| installments.py:191 | RELEASE_PENDING_AUTOPAY | Waiting for the payment provider to move the lease's rent autopay past this month. | provider |
| installments.py:195 | RELEASED | Released at the payment provider; {count} installment requests queued. | provider, queued |
| installments.py:196 | RELEASED_PLAN_CLOSED | Released at the payment provider. The plan is cancelled or completed: nothing more was queued. | provider, queued |
| installments.py:199 | RELEASED_PARENT_PAID | Released at the payment provider. A payment on the charge landed first: the plan needs attention and no installment was queued. | provider, queued |
| installments.py:203 | RELEASE_ALREADY | Already released at the payment provider. Nothing was changed. | provider |
| installments.py:210 | REQUEST_WAITING | A payment provider request of this charge is waiting: try again when it is done. | provider |
| installments.py:213 | NO_CUSTOMER | The operator has no payment provider customer: installments collected by {collection} are not possible. | provider |
| installments.py:221 | SHIFT_BUSY | A payment provider request of an installment is waiting: try again when it is done. Nothing was changed. | provider |
| installments.py:238 | CANCELLED | Plan cancelled. The unpaid installments are cancelled at the payment provider by the worker; the charge is open again for {balance}. | provider, worker |
| lease_end.py:196 | CONFIRM_REQUIRED | Tick the box to confirm that the lease ends with these effects. | tick |
| lease_end.py:219 | DOCUMENTS_DONE | Final statement stored; the e-mail to the operator is queued. | queued |
| transfer.py:139 | SETUP_TWICE | {name} is ticked twice. | ticked |
| transfer.py:143 | CONFIRM_REQUIRED | Tick the box to confirm the transfer with these effects. | tick |
| payments_entry.py:576 | SCHEDULE_NOTE | Scheduled from the store page: {item}. | scheduled |
| invoice_builder.py:261 | LINE_EXISTING_NOT_OPEN | Line {line}: {charge} is no longer open (paid, pending, billed, scheduled or on an installment plan). | scheduled |
| invoice_builder.py:300 | NO_PROVIDER_CUSTOMER | The operator has no payment provider customer: the invoice cannot be sent. Nothing was sent. | provider |
| invoice_builder.py:308 | SENT | Invoice {number} created. The payment provider e-mails it once it has the invoice; this page shows the outcome. | provider |
| invoice_builder.py:312 | CHARGE_REQUESTED | Invoice {number} created. The single debit is made once the payment provider has the invoice; this page shows the outcome. | provider |
| one_off_charges.py:248 | EDIT_ONE_OFF | Only a setup charge is edited here: its invoice is at the payment provider. Cancel the charge and create a new one. Nothing was saved. | provider |
| one_off_charges.py:259 | WAIVE_LIVE_INVOICE | This charge has a live Stax invoice. Cancel it first (Stax deletes the invoice), then waive if anything is still owed. | **Stax** |
| one_off_charges.py:264 | CANCEL_MEANING | removes the Stax invoice and closes the charge, nothing owed | **Stax** |
| one_off_charges.py:267 | WAIVE_MEANING | closes a charge that has no Stax invoice without collecting it | **Stax** |
| one_off_charges.py:271 | WAIVED_LIVE_HEADING | Waived with a live Stax invoice | **Stax** |
| one_off_charges.py:273 | DELETE_INVOICE_HINT | This charge is waived but its Stax invoice is still live. The charge stays waived. | **Stax** |
| one_off_charges.py:281 | DELETE_INVOICE_NONE | This charge has no live Stax invoice. Nothing was changed. | **Stax** |
| one_off_charges.py:284 | DELETE_INVOICE_SHARED | This Stax invoice also bills charges that are not waived: cancel it from its first line's charge. Nothing was changed. | **Stax** |
| one_off_charges.py:288 | DELETE_INVOICE_REQUESTED | Delete requested. The invoice is marked void once the payment provider confirms the delete; the charge stays waived. | marked, provider |
| one_off_charges.py:292 | WAIVED_INVOICE_DELETED | The provider invoice was deleted. The charge stays waived. | provider |
| one_off_charges.py:295 | TOTAL_MISMATCH | The invoice total at the payment provider differs from the price. The price was not changed. | provider |
| one_off_charges.py:298 | SEVERAL_PROVIDER_INVOICES | Several invoices at the payment provider could be this charge's. Nothing was created; review them on the payment provider's dashboard. | provider |
| one_off_charges.py:302 | OTHER_OPERATOR_REFERENCE | An invoice at the payment provider names this charge's invoice number but another operator's customer. It was not linked. | provider |
| one_off_charges.py:306 | SECOND_PROVIDER_INVOICE | More than one invoice at the payment provider names this charge. Only the first is linked; review the others at the payment provider. | provider |
| one_off_charges.py:324 | NO_PROVIDER_CUSTOMER | The operator has no payment provider customer: only a manual payment is possible. | provider |
| one_off_charges.py:330 | CREATED_NO_INVOICE | Charge created. The payment provider schedules the invoice for {date}. | provider, schedules |
| one_off_charges.py:332 | CREATED_MANUAL_LOCAL | Charge created as {number}. The operator has no payment provider customer, so no invoice is made there; record the payment when it arrives. | provider |
| one_off_charges.py:360 | CANCEL_BUSY | A request to the payment provider is already waiting for this charge. Nothing was changed. | provider |
| one_off_charges.py:363 | CANCEL_REQUESTED | Cancel requested. The charge is voided once the payment provider confirms the delete. | provider |
| one_off_charges.py:394 | CHARGE_NOW_NOT_LINKED | The charge's invoice is not at the payment provider yet. Charge now is possible once it is linked. | provider |
| one_off_charges.py:407 | CHARGE_NOW_BUSY | A request to the payment provider is already waiting for this charge. Nothing was charged. | provider |
| one_off_charges.py:431 | INVOICE_RELEASED | Invoice {number} was released at the payment provider for the installment plan. | provider |
| rent_run.py:181 | MATCH_OTHER_SCHEDULE | The invoice does not name this schedule as its source; not linked. | schedule |
| rent_run.py:185 | MATCH_CHARGE_INVOICED | The charge for {period} debit {seq} already has an invoice; not linked. The charge is flagged for review. | flagged |
| rent_run.py:192 | MATCH_MISMATCH | Linked to the charge for {period} debit {seq} as {number}. The invoice total at the payment provider differs from the amount due; collection on this charge stops until staff resolve it. | provider |
| rent_run.py:197 | MATCH_FAILED | The payment provider could not list the invoices of a schedule. Matching stopped. | provider, schedule |
| rent_run.py:198 | TOTAL_MISMATCH | The invoice total at the payment provider differs from the amount due. Collection on this charge stops until staff resolve it; the amount due was not changed. | provider |
| rent_run.py:202 | SECOND_INVOICE | The payment provider generated more than one invoice for this charge. Only the first is linked; review the others at the payment provider. | provider |
| rent_run.py:237 | MARK_DONE | The invoice at the payment provider is marked paid. | marked, provider |
| rent_run.py:250 | LINK_CONFIRM | The invoice total differs from the balance: tick the confirmation to link it anyway. Nothing was linked. | tick |
| rent_run.py:259 | LINK_DONE_DIFFERS | Invoice {number} linked to this charge. Its total differs from the balance; the charge is flagged for review. | flagged |
| leases.py:157 | NOT_PROVIDER_CHANNEL | The payment channel is not ACH debit through the payment provider. | provider |
| leases.py:159 | DAY_NOT_SCHEDULABLE | Debit days 29 to 31 cannot be scheduled yet (TODO-VERIFY-036). Choose a day from 1 to 28. | **TODO-VERIFY code shown**, scheduled |
| leases.py:163 | NO_PROVIDER_CUSTOMER | Operator has no payment provider customer. | provider |
| leases.py:168 | SCHEDULE_DIFFERS | Schedule amount differs from rent; pause and re-enable autopay. | schedule |
| leases.py:171 | PAUSE_FIRST | A schedule no longer matches the lease. Pause autopay first, then enable it again. | schedule |
| leases.py:172 | RESUME_REFUSED | A schedule no longer matches the lease, so it cannot be resumed. Enable autopay again. | schedule |
| leases.py:176 | NOTHING_TO_PAUSE | No active schedule to pause. | schedule |
| leases.py:177 | NOTHING_TO_RESUME | No paused schedule to resume. | schedule |
| leases.py:179 | NOTHING_TO_STOP | No active or paused schedule to stop. | schedule |
| leases.py:180 | NOTHING_TO_MOVE | No active rent autopay to move: nothing was changed at the payment provider. | provider |
| leases.py:183 | AUTOPAY_STOPPED | Autopay stopped: the provider schedules are deleted. | provider, schedules |
| leases.py:185 | METHOD_NOT_FOUND | That payment method is not one of this operator's methods at the payment provider. Nothing was created. | provider |
| leases.py:190 | METHOD_DELETED | That payment method is deleted at the payment provider. Nothing was created. | provider |
| leases.py:191 | SEVERAL_MATCHES | Several schedules at the payment provider match a debit whose last create failed. Nothing was created; review them in the provider's dashboard. | provider, schedules |
| leases.py:195 | PROVIDER_REJECTED | The payment provider refused the request. | provider |
| leases.py:196 | PROVIDER_FAILED | The payment provider could not be reached or did not answer. Try again later. | provider |
| leases.py:198 | LIMIT_EXCEEDED | Exceeds the confirmed provider limit of {limit}. Autopay cannot be enabled. | provider |
| refunds.py:90 | NOT_ACH | Only an ACH payment made through the payment provider can be refunded or voided here. A wire, Zelle or other payment was never a provider transaction. | provider |
| refunds.py:96 | NO_REFERENCE | The payment has no payment provider transaction on record. | provider |
| refunds.py:107 | CONFIRM_REQUIRED | Tick the confirmation to continue. Nothing was sent. | tick |
| refunds.py:109 | REFUNDED | Refund of ${amount} made at the payment provider and recorded. | provider |
| refunds.py:110 | REFUND_PENDING | It stays pending until the provider settles it. | provider |
| refunds.py:111 | VOIDED | Payment voided at the payment provider and recorded. | provider |
| refunds.py:112 | CALL_FAILED | The payment provider did not {verb} the payment ({failure}{fields}). Nothing was changed. | provider; `{failure}` is a SyncFailure-style code |
| refunds.py:115 | OUTCOME_UNKNOWN | The provider could not be reached, so the {noun} may have been made: check the payment provider before trying again. | provider |
| refunds.py:119 | REFETCH_FAILED | The payment provider could not be read back afterwards ({failure}); the record is completed when its event arrives. | provider |
| refunds.py:125 | REFUND_SENTENCE | A refund is sent to the payment provider at once, returns the money to the operator through the payment provider and cannot be undone here. | provider |
| refunds.py:129 | VOID_SENTENCE | A void cancels the whole payment at the payment provider before it moves money and cannot be undone here. | provider |
| collections.py:106 | HAND_QUEUED | The e-mail is queued; the log on the operator's page shows when it is sent. | queued |
| collections.py:107 | HAND_NEEDS_CONFIRM | This step was already sent for this charge. Tick the confirmation to send it again; it is recorded as a manual send. | tick |
| disputes.py:134 | REFRESH_QUEUED | Refresh queued. The list updates when the payment provider has answered. | provider, queued |
| disputes.py:135 | BUSY | A refresh is already waiting for the payment provider. Nothing was sent. | provider |
| disputes.py:136 | EVIDENCE_QUEUED | Evidence stored. It is sent to the payment provider in the background. | provider |
| disputes.py:145 | PAST_RESPOND_BY | The respond-by date of this dispute has passed: the payment provider no longer takes evidence. Nothing was saved. | provider |
| disputes.py:150 | NOTICE_REQUIRED | An image cannot be checked for account numbers. Tick the box to confirm it is a receipt, lease page or correspondence, not a bank form. Nothing was saved. | tick |
| disputes.py:155 | NO_PROVIDER_DISPUTE | The dispute has no payment provider reference. | provider, reference |
| disputes.py:156 | SENT | Evidence sent to the payment provider. | provider |
| disputes.py:158 | REFUSED | The payment provider refused the evidence ({error}). | provider |
| disputes.py:164 | SYNC_MORE | More pages than read in one sync; the rest is read next time. | sync |
| insurance.py:65 | NOT_MARKED | Provider invoice not updated: an insurance invoice is marked paid by hand | provider, marked |
| nsf.py:84 | NO_PROVIDER_CUSTOMER | The operator is not linked to the payment provider: nothing can be re-debited or invoiced. Nothing was changed. | provider |
| reports.py:218 | CARDS_NOTE | Cards do not exist in this app. A manual payment marked at the payment provider as cash or check is recorded here as Zelle, wire or other, with a note. | marked, provider |
| pnl.py:180 | UNREADABLE | The months could not be read (use YYYY-MM); showing {start} to {end}. | YYYY-MM format |
| views/invoices.py:94 | BAD_MONTH | The month must be YYYY-MM. | YYYY-MM format |
| store_costs.py:629 | NOT_CSV | The file is not a CSV in UTF-8 text: save it as "CSV UTF-8" or as .xlsx. Nothing was imported. | CSV, UTF-8 |
| store_costs.py:840 | ROW_DATE | {column} is not a date (MM/DD/YYYY or YYYY-MM-DD) | format |
| store_costs.py:841 + :896 | ROW_TOO_LONG | {column} is longer than {limit} characters. Called with `column="other_cost_description"` / `"notes"`, so a **raw field name** is shown | field name |
| views/jobs.py:23 | BUSY | A request to the payment provider is already waiting for this record. Nothing was sent. | provider |
| views/reconciliation.py:57 | RUN_QUEUED | Reconciliation queued. Its state appears below; the report follows when it is done. | reconciliation, queued |
| views/run.py:44 | MATCH_QUEUED | Matching queued. The report appears below when it is done. | queued |
| credits.py:57 | CREDIT_SHORT | The credit held is less than the credit applied. Nothing was recorded. | N |

`search.py` has no sentence constants; its constants are only limits and patterns.

Plain-sentence constants not listed (count per file): installments 33, lease_end 37, transfer 35, payments_entry 16, invoice_builder 52, one_off_charges 65, rent_run 35, leases 24, store_costs 37, contacts 24, deposits 23, collections 14, disputes 12, refunds 12, nsf 12, calendar 9, audit 8, pnl 8, attention 5, returns 5, reports 4, receivables 4, workspace 4, others 1-3 each.

### 5b. Stax named in any file (user-visible)

| Where | Current text | Note |
|---|---|---|
| payments/__init__.py:21 | PROVIDER_LABEL = "Stax" | Used in the seven one_off_charges.py sentences above |
| ledger/templates/ledger/documents/_notice.html:1 | Do not upload bank forms, ACH authorisations, voided cheques or card details. Those stay in Stax or DocuSign. | Stax, ACH |
| payments/offline_marking.py:39-45 | Stax invoice not updated: partial amount (TODO-VERIFY-028) / Stax invoice not updated: the payment differs from the invoice total / Stax invoice already showed paid before marking; not marked again. Check for a double payment / Stax invoice not marked paid: {failure} / Stax invoice marked paid but not re-read: {failure} | `shown()` (line 81) swaps "Stax" for "Provider" and removes the TODO before display, so staff see "Provider invoice …" (still: mark, re-read) |

### 5c. Template sentences with technical terms (visible text)

| Template | Current text (truncated) | Term(s) |
|---|---|---|
| _job_status.html | Payment provider request ({job kind}, {time} UTC): … This page refreshes every 5 seconds. | provider, UTC |
| dashboard/home.html | None. An invoice whose last provider status is ATTEMPTED is listed here. | provider, ALL CAPS |
| dashboard/home.html | Last reconciliation / No reconciliation has run yet. / …, started … UTC | reconciliation, UTC |
| disputes/page.html | Last synced … UTC. Not synced yet. | synced, UTC |
| leases/detail.html, stores/_tab_schedules.html | Provider schedule / Autopay schedules / Next debit is computed here from the debit day; the provider runs the schedule. | provider, schedule |
| leases/enrol.html | One payment provider schedule is created per debit day. … / First debit (DTSTART) / Methods as of … UTC | schedule, **DTSTART**, UTC |
| leases/form.html | … A lease with autopay keeps its schedules; a … flagged … | schedules, flagged |
| leases/stop_autopay.html | Stop autopay: the provider schedule is deleted; re-enrol to start again. | provider, schedule |
| one_off_charges/cancel.html | … The payment provider's invoice (or, for autopay on the due date, its schedule) is deleted; the provider then shows the invoice as deleted, as it has no "void". | provider, schedule |
| one_off_charges/detail.html | Legacy (badge) | legacy |
| charges/link_invoice.html, one_off_charges/link_invoice.html | The invoices of this lease's operator that are not linked … payment provider's dashboard … flagged … tick | provider, flagged, tick |
| charges/payment_form.html, one_off_charges/payment_form.html, stores/record_payment.html | A payment received by Zelle, wire or another way outside the payment provider … marked … | provider, marked, flagged |
| lease_ends/confirm.html, transfers/confirm.html | Read what will happen and tick the box … / Nothing is sent to the payment provider. | tick, provider |
| lease_ends/blockers.html, transfers/blockers.html | Nothing is live at the payment provider … | provider |
| operators/detail.html | Retry sync / Provider customer updated … UTC / SMS phone / SMS consent given on | sync, provider, UTC, SMS |
| reconciliation/page.html | Each night the reconciliation re-reads open invoices … and flags pending payments … / Runs the same reconciliation as the nightly job, now. … | reconciliation, flags, job |
| reports/deposits.html | Open findings of the nightly reconciliation's deposit step … reconciliation page | reconciliation |
| reports/pnl.html | Flags (column) | flags |
| run/today.html | … webhook event… dead-lettered / The payment provider's schedules generate the invoices … / "Match invoices" reads the invoices of the provider's schedules only … | webhook, dead-letter, schedules, provider |
| stores/_tab_history.html | … charges, invoices, payments, schedules, documents … | schedules |
| webhooks/detail.html | Payload (redacted) / No payload stored: the body was not JSON, or nothing of it could be kept without card or account data. | payload, JSON, webhook |
| audit/page.html | Time (UTC) | UTC |

About 127 more mentions of "payment provider" / "provider" are spread over 45 templates.

### 5d. `ledger/forms.py` labels and help texts

| Line | Kind | Current text | Technical term? |
|---|---|---|---|
| 148 / 153 | label | Customer ID / Payment method ID | Y (provider IDs, see LinkForm) |
| 175 | help | Only for a lease debited twice a month. | N |
| 178 / 185 | label | Monthly rent / New rent takes effect on | N |
| 188 | help | Only used when the rent changes. Empty means today. | N |
| 385 / 399 / 446 | label | Vendor / Due date / Payment method | N |
| 522 | label | Received on | N |
| 528 | help | Optional: the bank or Zelle reference. Letters, digits and hyphens of it (up to 25) are sent with the invoice when it is marked paid. | Y (ref, marked) |
| 536 | help | Required: what was received, and any reference staff need. | Y (reference) |
| 569 / 570 | label/help | Amount received / Leave empty to apply the credit held alone. | N |
| 576 | label | Keep the rest as a credit on the lease | N |
| 612 / 619 | help | Pick a catalogue item from the list or type a name. / Optional: a detail shown after the item. | N |
| 625 / 627 | label/help | Debit on / Today or later: the operator's bank account is debited on this date. | N |
| 655 | help | Required when the total differs from the balance: say why. | N |
| 671 | help | Required: how the return was resolved. | N |
| 734 / 740 | label/help | Lease / Shown on the invoice. | N |
| 747 | help | Empty: {DUE_DAYS} days after the invoice is sent (New York time). | N |
| 811 / 827 | label/help | Item / Optional detail under the item; at most {n} characters. | N |
| 837 / 842 / 843 / 844 | label | Unit amount / Save as catalogue item / Rent: lease / Rent: month | N |
| 1006 / 1024 / 1043 | help | Required: why the charge is cancelled. / … waived. / Required: how the mismatch was resolved. | N |
| 1072 | label | Refund amount | N |
| 1098 / 1102 / 1108 / 1132 / 1147 | label | Effective from / Other cost description / Landlord lease end / Why / Effective to | N |
| 1150 | help | The last day this amount applies. Empty means it still applies. | N |
| 1181 / 1183 | label/help | Last day / The last day the current landlord rent applies. | N |
| 1205 / 1209 / 1217 | label | Resolution / Date / Transfer to lease | N |
| 1213 | help | Refunded only: how the money was paid back. | N |
| 1219 | help | Transferred only: another lease of the same operator that has not ended. | N |
| 1225 | help | Required: what happened to the deposit, and why any part was kept. | N |
| 1255 / 1257 | label/help | End date / The last day the operator had the store: not before the lease started, not in the future. | N |
| 1263 | help | Why the lease ends; Other needs a few words below. | N |
| 1266 / 1270 | label/help | Reason in words / Optional, except for Other; never a card or account number. | N |
| 1273 / 1276 | label/help | Prorate last month / Charge the last month for the days up to the end date only. | N |
| 1301 / 1304 | label | Description / Amount | N |
| 1352 / 1355 | label/help | Refund method / How the refund is paid: Zelle, wire or check, never the payment provider. | Y (provider) |
| 1358 / 1361 | label/help | Refund date / The day the refund is sent; not in the future. | N |
| 1364 / 1368 | label/help | Reference / A Zelle confirmation, a wire reference or a check number; no account number. | Y (reference) |
| 1374 | help | Anything staff should know about the settlement. | N |
| 1380 | label | I have read the effects above and want to end the lease | N |
| 1406 / 1408 | label/help | Start date / The first day of the incoming lease: the day after the outgoing lease ends, or later. | N |
| 1418 / 1420 / 1427 | label | Debit day / Second debit day / Payment channel | N |
| 1434 | help | How the incoming operator pays the rent. | N |
| 1437 / 1440 | label/help | Prorate first month / When the lease starts after the 1st, the first month's rent is charged for the days used only. | N |
| 1444 / 1450 | label/help | Security deposit / Paid by the incoming operator; zero or empty for none. It goes on the move-in invoice draft. | N |
| 1486 | label | I have read the effects above and want to transfer the store | N |
| 1501 | help | Required: why the balance may remain on the outgoing operator. | N |
| 1529 / 1531 / 1576 / 1563 | label | Name / File / Kind / Take photo | N |
| 1536 | label | This is a receipt, lease page or correspondence, not a bank form | N |
| 1635 | help | New York date. | N |
| 1639 / 1641 | label/help | Promised date / Promise to pay only. | N |
| 1645 | label | Promised amount (USD) | N |
| 1653 / 1686 / 1687 / 1690 | label | Note / Store / Previous promise / What happened to it | N |
| 1779 / 1791 / 1814 | label | Correction / Reason / Text | N |
| 1821 / 1822 | label/help | E-mail me that morning / An e-mail to you on the morning of that day (09:00 New York). | N |
| 1825 / 1878 / 1892 | label | Operator / First installment's date / Number of installments | N |

## 6. Money shown without a thousands separator

`|floatformat:"2g"` groups digits, so these are fine: dashboard/home.html:44-46 (the three figures), arrears/page.html:39,41,65,66, grid/page.html:105, invoices/_draft_form.html:46, leases/form.html:40, transfers/incoming.html:31, lease_ends/statement.html:23. Python `format_amount` / `proration.money` / grid `_amount` / calendar.py:748 use `:,`, so they are fine too.

**No separator: plain `${{ x }}` in templates** (`ledger/templates/ledger/`, line numbers):

| Template | Lines |
|---|---|
| arrears/page.html | none (all use floatformat) |
| charges/detail.html | 33, 35, 36, 37, 49, 67 |
| charges/link_invoice.html | 7, 27 |
| charges/payment_form.html | 7 |
| contacts/_entry.html | 6 (promise amount) |
| operators/_contacts_table.html | 23, 31 (promise amount) |
| dashboard/home.html | 30, 72, 89 (list rows below the figures) |
| deposits/resolve.html | 6 |
| disputes/page.html | 30 |
| installments/_installments.html | 7 |
| installments/split.html | 9 |
| invoices/_invoice.html | 30, 33 |
| invoices/charge_now.html | 9, 22 (button "Charge ${total} now") |
| invoices/draft.html | 13, 28 (button "Send ${total} by e-mail"), 43, 46 |
| lease_ends/_effects.html | 3, 6, 8, 9, 10, 11, 12 |
| lease_ends/_figures.html | 5, 6, 12, 22, 27, 32, 33, 38, 39, 45, 46, 51-56 |
| lease_ends/settlement.html | 7, 8 |
| leases/detail.html | 36, 37, 46, 72 |
| leases/enrol.html | 23 |
| leases/list.html | 26 |
| leases/stop_autopay.html | 19 |
| one_off_charges/cancel.html | 6 |
| one_off_charges/charge_now.html | 9, 30 |
| one_off_charges/detail.html | 65, 66, 67, 68, 84, 87, 98, 118 |
| one_off_charges/link_invoice.html | 7, 27 |
| one_off_charges/payment_form.html | 7 |
| one_off_charges/waive.html | 6 |
| payments/detail.html | 7 (H1), 28, 41, 56, 58, 69, 70, 71, 78, 79, 80, 85 |
| payments/list.html | 49-53 (totals), 74 |
| payments/refund.html | 18 |
| payments/summary.html | 9, 11, 12 |
| returns/list.html | 18 |
| run/today.html | 44, 73, 74, 76, 85, 116 |
| stores/_header.html | 5, 7, 8, 9, 11 |
| stores/_tab_charges.html | 19, 20, 21, 32 |
| stores/_tab_invoices.html | 12, 40, 41 |
| stores/_tab_payments.html | 16, 39, 40, 41 |
| stores/_tab_schedules.html | 15, 34 |
| stores/charge_now_pick.html | 13 |
| stores/cost_correct.html | 6 |
| stores/cost_form.html | 6 |
| stores/detail.html | 53, 55, 56, 57, 64, 80, 84 |
| stores/record_payment.html | 20, 30, 40 |
| stores/schedule_payment.html | 21 |
| transfers/confirm.html | 13, 17, 18, 32 |

**No separator: Python sentences:**

| File:line | Text |
|---|---|
| ledger/services/contacts.py:235 | `f"Closed ${promise.promised_amount} by {date} as {status}"` (the "Closed … as …" sentence on the contact log) |
| ledger/services/workspace.py:748 | `f"{Refunded/Withheld/Transferred} ${amount.quantize(CENT)}"` (deposit resolution in the store Charges tab) |
| ledger/services/deposits.py:86 | ABOVE_PAID "… (${paid}) …", filled at :391 with a raw Decimal |
| ledger/services/deposits.py:94-95 | SPLIT_NOT_WHOLE "… (${paid}) …", filled at :337 |
| ledger/services/refunds.py:104 | AMOUNT_OVER "… ${refundable} …", filled at :259 and views/payments.py:194 |
| ledger/services/refunds.py:109 | REFUNDED "Refund of ${amount} …", filled at :309 |
| ledger/services/refunds.py:134 | VOID_CONFIRMATION "I confirm the void of this whole payment of ${amount}.", filled at :229 |
| ledger/services/invoice_builder.py:1257 | line text `({qty} × {unit_amount})`, raw Decimal with no "$" |
| ledger/services/grid.py:286-295 | Has the separator, but no "$" ("paid 2,000.00") |

## 7. Empty states (template: text)

| Template | Text |
|---|---|
| _attention.html | Nothing needs attention. |
| arrears/page.html | No operator is behind. |
| audit/page.html | No audit rows match. |
| charges/detail.html | No invoice linked yet. / No notes. / No payments. |
| charges/link_invoice.html, one_off_charges/link_invoice.html | No unlinked invoice of this operator. |
| collections/_log.html | No collection e-mails. |
| dashboard/home.html | None. / No reconciliation has run yet. / None. An invoice whose last provider status is ATTEMPTED is listed here. (Y: provider, ALL CAPS) |
| disputes/page.html | No disputes / None sent. |
| documents/_section.html | No documents. / No document activity yet. |
| grid/page.html | No store matches. |
| lease_ends/_figures.html | Nothing is open. |
| lease_ends/blockers.html | Nothing is live at the payment provider for this lease. (Y: provider) |
| leases/detail.html | No notes. / No rent recorded. |
| leases/enrol.html | No rent is in effect on the first debit date. |
| leases/form.html | No item is shown in the setup checklist. |
| leases/list.html | No leases. |
| one_off_charges/detail.html | No invoice linked. / No payments. |
| operators/_contacts_table.html | No contact entries. |
| operators/detail.html | No notes. / No store has a lease of this operator. / No store. |
| operators/list.html | No operators match. |
| payments/detail.html | No audit rows. / No charge linked. / No invoice. / No refunds. |
| payments/list.html | No payments match. |
| reconciliation/page.html | No mismatches. / No reconciliation has run yet. / No runs yet. / No step completed. (Y: reconciliation) |
| reports/aging.html | Nothing is owed. |
| reports/deposits.html | No deposit in these months. / No open finding. |
| reports/pnl.html | Nothing billed, collected or held in these months. |
| reports/statement.html, statement_public.html | No payment in these months. / Nothing is owed. |
| returns/list.html | No unresolved returns. |
| run/today.html | No debit is due today. / No insurance is due in the next {n} days / None. / No charge / Not created / Not yet created |
| search/results.html | No match for '{q}' / No store matches. / No operator matches. / No operator |
| stores/_tab_history.html | No history yet. |
| stores/_tab_invoices.html | No draft. |
| stores/_tab_payments.html | No payments. / No credit held or used. / Not used yet |
| stores/_tab_schedules.html | No live plan. / None. |
| stores/charge_now_pick.html | No charge of this store can be charged now. / Not chargeable now |
| stores/cost_form.html | No landlord rent is recorded for this store yet: its cost is unknown. |
| stores/detail.html | No leases. |
| stores/list.html | No stores match. |
| stores/record_payment.html | No open charge: the whole amount can be kept as a credit on the lease. |
| transfers/blockers.html | Nothing is live at the payment provider and nothing is owed on this lease. (Y: provider) |
| transfers/confirm.html | No deposit and no setup charge: no invoice draft is made. / Nothing is sent to the payment provider. (Y: provider) |
| transfers/operator.html | No operator matches. Create one below. |
| webhooks/detail.html | No payload stored: the body was not JSON, or nothing of it could be kept without card or account data. / Not processed (Y: payload, JSON) |
| webhooks/list.html | No webhook events match. (Y: webhook) |

## 8. Attention list texts (`ledger/services/attention.py`)

| Line | Constant / source | Current text | Technical term? |
|---|---|---|---|
| 182 | RETURNED_TEXT | Returned debit not rescheduled: {store}, {amount}, returned {date} | N |
| 183 | INSTALLMENT_RETURNED_TEXT | Installment returned: {store}, {parent}, plan {paid} of {count} paid | N |
| 184 | SPLIT_CHARGE_PAID_TEXT | Payment on a split charge: {store}, {parent}, {amount} paid before its invoice was released; cancel or reschedule the plan | N |
| 188 | LEASE_END_BALANCE_TEXT | Lease-end balance: {operator} on store {label}, {amount} open since {ended_on} | N |
| 191 | LEASE_END_BALANCE_ALLOWED | (transfer, allowed by {name}) | N |
| 428 | fallback | No operator | N |
| 242 | inline | Overdue {n} day(s): {where}, {description}, {amount} | N |
| 457 | inline | Promise to pay {amount} by {date}: {operator}[ on store {label}: owes {amount}], not kept | N |
| 536/538/554 | inline | Insurance premium for {year} missing, due {date}: {where} / Insurance due {date}: {where} | N |
| 569 | inline | Lease ends {date}: {where} | N |
| 623 | inline | Landlord lease ends {date}: {store} | N |
| 639 | inline | Store without an open date: {store} | N |
| 655 | inline | Operator without an e-mail address: {operator} | N |
| 700 | inline | Vacant since {date}: {store} | N |
| 718 | inline | Dead-letter webhook events: {count} | Y (dead-letter, webhook) |
| 731 | inline | Unresolved reconciliation mismatches: {count} | Y (reconciliation) |
| 744 | inline | {Note/Reminder} today: {title} | N |
| _attention.html | heading / footer | Needs attention: {n} / And {n} more {label}. / since {date} | N |
| 154-172 | KIND_LABELS (used in "And {n} more …") | overdue charges; returned debits; installment plans with a returned debit; split charges paid before their invoice was released; lease-end balances open; broken promises; insurance premiums missing; insurance due soon; leases ending; landlord leases ending; **schedules to review** (Y: schedule); **dead-letter webhooks** (Y); **reconciliation mismatches** (Y); vacant stores; stores without an open date; operators without an e-mail; calendar entries today | see items |

Amounts in the attention list use `format_amount`, which already has the comma.
# PLAN-010d — One-off charges and invoices from the app; "Charge now"; the audit log page

**Date:** 2026-10-05 · **Author:** Fable (QA owner) · **Implementer:** Opus
**Origin:** owner's Prompt 10d text of 2026-10-05, and the owner's 10c decision that the audit
log page is built here.

## 1. Restated objective

Every amount that is not monthly rent (old arrears, an adjustment, damage, later NSF fees and
installments) has no entry point in the app: today staff create the invoice in the Stax dashboard
and the app links it. This step gives staff one "Create one-off charge" action on the operator
and lease pages that writes the charge in our ledger and, through the worker, creates the Stax
one-off invoice for the operator with our invoice number as its reference and the description
as its line item, then collects it the way the form chose: autopay on the due date from the
operator's linked bank method, sent by Stax email, left unsent for a manual payment, or charged
now. "Charge now" is the owner's decision to use Stax's pay-an-invoice call with a stored method,
admin only, one attempt, behind a confirmation page. An unpaid one-off charge can be cancelled,
which deletes the Stax invoice (Stax has no "void" for an invoice; a deleted invoice reads
`DELETED`) and marks the charge voided. Prompt 10b's setup charges become one-off charges of
category `setup`, so this is the single creation path that 11b's NSF fees and 12b's installments
will use. The audit log gets its admin page at `/audit/` with filters and CSV, and each Documents
section gets a short Activity list.

## 2. Affected packages

`backend/ledger` (models, migrations, services, forms, views, templates, URLs), `backend/payments`
(job kinds and runners, a new `one_off_invoices` module, the adapter's `pay_invoice` retry rule,
the invoice webhook handler's linking), `backend/accounts/registry.py`, `backend/tests`, `docs/`
(ADR-0009; docs/CLAUDE.md Rule 2 amended by the owner's decision). **No infrastructure change, no
new dependency.** Out of scope: reminders (no email provider until Phase 7), NSF fees and
installments themselves (11b, 12b), `charge_adjustments` (kept for cross-store balance moves;
reviewed in Step 13), card payments of any kind.

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **`setup_charges` becomes `one_off_charges`** (model `OneOffCharge`, table renamed; the payment and invoice FK columns, the payment kind `setup` → `one_off`, the job kinds, the audit entity type and the URL prefix renamed with it; one data migration rewrites existing audit, job and payment rows). New columns: `operator` FK (always set; copied from the lease when there is one), `lease` FK now nullable (operator-level charges), `category` in {`setup`, `arrears`, `adjustment`, `damage`, `other`} (CHECK: `fee_type` set iff `setup`), `description` (the invoice line, guarded, ≤ 120 characters, required for non-setup; for setup defaults to the fee type's name), `collection` in {`none`, `autopay_due`, `email`, `manual`, `charge_now`} (`none` only for `setup`/legacy rows that follow 10b's flow), `voided_at`, `voided_by`, `void_note`, `charge_now_key` (UUID, set once; the idempotency ID of the single pay attempt), status gains `voided`. Everything 10b built (checklist, pages, record payment, link, waive, edit, retry, P&L, ledger, deposits) keeps working on the renamed model; setup charges are created through the same `create_charge` service with category `setup` and collection `none` | Owner items 1, 4 |
| D2 | **Creation** (`ledger/services/one_off_charges.create_charge`, billing; admin for `charge_now`): one transaction writes the charge and, for collections `email`, `manual` and `charge_now`, a local `Invoice` row first (number from `next_invoice_number`, `internal_status=draft`, linked to the charge, `legacy=False`), then queues the job `invoice_create`. For `autopay_due` no invoice row is written: the job `schedule_create` creates a Stax scheduled invoice with the linked bank method and an RRULE of **one occurrence** on the due date (`DTSTART=<due date>;FREQ=DAILY;COUNT=1`), `meta.reference` = a reference the charge owns (`OOC-<short id>`), and Stax generates the invoice on that date; its `create_invoice` webhook links to the charge by schedule reference (D5). The large-debit confirmation of PLAN-007 applies to `autopay_due` and `charge_now` above `LARGE_DEBIT_WARNING_AMOUNT` (confirmation box, audited); `STAX_MAX_TXN_AMOUNT` refuses above the hard limit when set. Form refusals: operator without a current Stax customer reference for anything but `manual`; `autopay_due`/`charge_now` without a linked bank method; due date in the past for `autopay_due`; amount ≤ 0 or > 999,999.99; description or note empty after `visible_text`; payment data anywhere | Owner item 1; TODO-VERIFY-019's working assumption; D1-B |
| D3 | **Stax invoice creation in the worker** (`payments/one_off_invoices.py`, job `invoice_create`, entity the charge): local-first as Rule 5: the invoice row exists; the job calls `create_invoice` with `InvoiceInput(customer, Amounts(total, subtotal, tax 0, one LineItem(description, 1, amount)), due_on, invoice_date today, memo "", reference=<our invoice number>)`; on success writes the `external_refs` row, copies the provider state through the sync writer, sets `internal_status=ready`, and then for `email` calls `send_invoice_email` (status → `sent` when the webhook or the re-fetch says so). **Check-before-create** on any retry: list the customer's invoices of the last 7 days and take the one whose `meta.reference` equals our number (if the provider returns it; TODO-VERIFY-004) or, failing that, the one whose total and date match uniquely; if none, create; if several, `needs_review` and stop. A `4xx` is never retried; the charge page shows "Failed: <class> (fields)" and the same single manual retry as everywhere (a new job). The charge page shows the Stax status as the setup page does | Owner item 1; Rule 5 |
| D4 | **"Charge now"** (owner decision; ADR-0009; docs/CLAUDE.md Rule 2 amended): admin only; a confirmation page repeating operator, store, amount, our invoice number and the **last four of the linked bank method** (from the stored methods snapshot; never fetched by the page), the large-debit box when due, and the sentence "One attempt. If it fails you will see why and can try again later." On confirm the job `invoice_pay` runs `pay_invoice(PayWithStoredMethod(method, idempotency_id=charge_now_key, email_receipt=True))` **exactly once**: the adapter's retry policy for this call is none at all (no network, no 429, no 5xx retry; a timeout counts as unknown and the next attempt reuses the same idempotency ID, which is what the ID is for). The result is a `TransactionRecord` applied through the transaction handler: a pending ACH `one_off` payment that settles through webhooks and the reconciliation like any other. A failure is shown with its class and field names and leaves the charge open; the button returns after one minute. `STAX_PAY_INVOICE_ENABLED` gates the adapter call: when off (staging today, until Stax enables ACH) the page shows "Charge now is not enabled in this environment" and no job is queued. Charging an invoice that Stax already shows paid is refused before the call (re-fetch first) | Owner item 1b |
| D5 | **Linking.** An incoming `create_invoice` webhook whose record carries our invoice number in `meta.reference`, or whose schedule reference is a one-off charge's, links to that charge and never auto-links to a rent charge. `list_invoices` matching in D3 uses the same reader. Requires `InvoiceRecord` to expose `reference` and `schedule_id` from the summary (both already stored in the trimmed summary; expose them, do not widen the summary) | Owner item 1; Rule 4 |
| D6 | **Cancel** (billing; note required; audited): allowed when `amount_paid` = 0 and no pending payment exists. For an invoice: job `invoice_delete` → `delete_invoice`; the local invoice becomes `void`, the charge `voided` with `voided_at/by/void_note`; Stax then reads `DELETED` (there is no "void" for an invoice; the owner's done-when reads "voided", meaning this state). For an `autopay_due` charge not yet fired: `delete_scheduled_invoice`. A charge with any payment follows the refund path and cannot be cancelled. A failed delete leaves the charge `owed` with the failure shown and the retry; the local void is written only after the provider confirmed (re-fetch) | Owner item 2 |
| D7 | **Consumers.** Payments, partials, manual payments with the Stax mark, refunds, voids, returns, the ledger, deposits, the P&L and the reconciliation treat every one-off charge as they treated setup charges. P&L mapping by category: `arrears` counts as **rent** billed and collected (it is rent owed); `setup`, `adjustment`, `damage`, `other` count as fees; refundable fee types stay deposits. Reminders: none exist until Phase 7 | Owner item 3 |
| D8 | **Audit log page** `/audit/` (admin; GET only; registry read-only rule with the admin floor checked in the view too): filters `action` (exact or prefix with `*`), `user`, `entity_type`, `entity_id` (prefix allowed), `from`/`to` dates (default last 7 days, cap 366), paginated newest first, 100 per page; each row shows time (UTC and New York), action, user, entity type, masked entity id, and `before`/`after` rendered as key/value with identifiers masked and money shown as text. **CSV export** of the filtered rows (cap 50,000; streaming; formula-safe cells; `*_json` columns as compact JSON). Links to the record page where one exists (operator, lease, store, charge, payment, document owner). **Activity list** in each Documents section: the last 10 `document.*` rows for that store or lease (uploads, previews, downloads, deletions and refusals with the reason code), newest first, with a link to `/audit/` prefiltered for admins | Owner item 5; 10c gap |
| D9 | **Roles:** create one-off charge = billing; `charge_now` = admin; cancel = billing; audit page = admin. The one-off charge pages keep 10b's rules | Pattern |

## 4. Interface contract

Models: `OneOffCharge` (+ `OneOffCategory`, `CollectionMethod`, status `voided`), `Payment.one_off_charge`,
`Invoice.one_off_charge`, `PaymentKind.ONE_OFF`, job kinds `invoice_create`, `schedule_create`,
`invoice_send`, `invoice_delete`, `invoice_pay`, `one_off_payment_mark`, `one_off_payment_retry_mark`.
Services: `ledger/services/one_off_charges.py` (`create_charge(operator, lease, ChargeInput, actor)`,
`cancel_charge`, `charge_now_request`, `charge_now_confirm`, the 10b functions renamed),
`payments/one_off_invoices.py` (`create_provider_invoice(charge_id, actor)`, `send(charge_id)`,
`delete(charge_id)`, `pay(charge_id, actor)`, `schedule(charge_id, actor)`), `InvoiceRecord.reference`,
`InvoiceRecord.schedule_id`, `ledger/services/audit.py` (`AuditQuery`, `rows`, `csv_lines`,
`activity_for(owner)`). URLs: `ledger:one_off_charges:*` (10b's names), `operators:new_charge`,
`leases:new_charge`, `one_off_charges:cancel|charge_now`, `audit:page|export`. Settings: none new.

## 5. Work breakdown

| Batch | Task | Done when |
|---|---|---|
| A | D1 rename and new columns (migrations with the data rewrite and the guard triggers); `create_charge` for every category with the invoice row and the jobs; `payments/one_off_invoices.py` with `invoice_create` (check-before-create), `invoice_send`, `schedule_create`, `invoice_delete`; D5 linking and `InvoiceRecord.reference|schedule_id`; the form on operator and lease pages; the charge page's status and cancel; the setup checklist and 10b pages on the renamed model; consumers (D7) and the P&L category mapping; tests | A $3,000 arrears charge with `email` queues the create job, the worker creates and sends the fake-provider invoice with our reference and the line item, the page shows it linked and sent; cancel deletes it and shows voided; setup charges still work end to end |
| B | D4 Charge now (adapter no-retry rule for `pay_invoice`, flag behaviour, confirmation page, job `invoice_pay`, transaction applied, failure shown, one-minute button rule); D8 audit page, CSV and Activity lists; registry; tests | Charge now with the fake provider yields a pending `one_off` payment; the flag off shows the sentence and queues nothing; a 503 is shown once and never retried; `/audit/` filters and exports; billing gets 403; the Activity list shows uploads, downloads and refusals |
| Docs (Fable) | ADR-0009 (pay-an-invoice for Charge now; one creation path; cancel = delete); docs/CLAUDE.md Rule 2 amendment; RUNBOOK §13; DATA_OWNERSHIP (table rename, new columns); TODO-VERIFY-019 update; README; QA-010d | |

A then B, in sequence (B touches the same views and registry).

## 6. Test plan (before implementation)

Happy paths: each category and collection through the form; the invoice row's number and the
provider call's body (customer, total, one line item with the description, `meta.reference` = our
number, ACH only, no card); email → `send_invoice_email` after creation; `autopay_due` → one
schedule with `COUNT=1` on the due date and the linked method; manual → nothing sent; the
webhook linking by reference and by schedule reference; cancel → delete call, local void, Stax
status `DELETED` after re-fetch; charge now → one `pay_invoice` with the stored key, a pending
payment, the charge page shows it; setup checklist unchanged. Boundaries: amount 0.01 and
999,999.99; description 120 characters; due date today; a lease that ended (allowed for arrears,
refused for `autopay_due`); an operator with two leases (the lease is chosen on the lease page,
blank on the operator page); large-debit box exactly at the threshold; `STAX_MAX_TXN_AMOUNT`.
Error paths: create 422 → failed, no retry, one provider call, retry button makes a new job;
create timeout → the retry lists first and finds the invoice by reference and creates nothing;
two candidates → needs_review; send fails after create → invoice linked, status ready, "send
failed" shown, retry sends only; cancel with a pending payment → refused; cancel delete 404 →
treated as already deleted; charge now with the flag off, without a method, above the hard
limit, on a paid invoice → each refused before any call; charge now 503 → one call, failure
shown, the key unchanged, a later attempt reuses it; two concurrent charge-now clicks → one call.
Adversarial (Fable): the description with a card number, HTML, 10,000 characters, RTL marks;
`meta.reference` collision (two charges, two numbers, a webhook naming the wrong one links
nothing twice); a webhook `create_invoice` whose reference matches but whose customer differs
→ not linked, flagged; replaying the create job after success creates nothing; `charge_now_key`
cannot be changed once set (model-level); billing POST to charge now → 403; viewer POST to
create → 403; cancel of a paid charge → refused; the audit page under a 10-year range, a
10,000-character filter, `entity_id` with `%`/`_`, an unknown user → 200 with a message and
bounded queries; the CSV never carries a full provider ID or a card number even if `after`
holds a masked one; a billing GET of `/audit/` and of the CSV → 403; the Activity list shows
nothing of another record; P&L: arrears counted as rent, damage as fees, a voided charge in
neither.

## 7. Risk register

- **Money movement initiated by the app** (Charge now): mitigated by admin-only, confirmation,
  one attempt with a stored idempotency ID, flag off until Stax enables ACH, and the re-fetch
  before paying. Recorded in ADR-0009.
- **Table and kind rename** touches payments, invoices, jobs, audit rows, URLs and the P&L;
  every consumer is enumerated in QA; a data migration rewrites rows; nothing is in production.
- **Check-before-create depends on `meta.reference` being returned** (TODO-VERIFY-004) with a
  total-and-date fallback; the staging click shows which. Duplicates on Stax are visible in the
  dashboard and reconcilable by deletion.
- **Stax "void" semantics:** an invoice is deleted, not voided; the owner is told what to expect.
- **Schedules with one occurrence** are TODO-VERIFY-019's assumption; verified only when ACH
  is enabled; the code path has unit tests.
- **Audit page over a large table:** filtered by indexed columns, capped ranges and page size,
  streaming CSV with a cap; the P&L statement timeout helper is reused.

## 8. Acceptance criteria

Gate green on both engines; migrations clean; every 10b test still passes on the renamed model;
on staging: a $3,000 arrears charge for operator halid with "send by email" appears with the
Stax invoice linked and status sent, the invoice is visible in the Stax dashboard with our
reference, a wire payment recorded in the app marks it paid in Stax with the memo, a second
charge cancelled shows `DELETED` in Stax; `/audit/` filtered by `document.uploaded` shows the
owner's uploads and a billing user gets 403; Charge now is proven by unit tests until Stax
enables ACH.

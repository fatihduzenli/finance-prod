# PLAN-012 — Contact log maturity, the invoice builder, the dispute list; backlog and provider documents

**Date:** 2026-10-07 · **Author:** Fable (QA owner) · **Implementer:** Opus
**Origin:** Prompt 12 (documents) with the owner's amendments of 2026-10-07 (which build three
things) and the owner's four contact-log findings from the 11b clicks. Application work only: no
new Stax write beyond the ones that exist (create invoice with several lines, send, cancel) and
**one new Stax read** (list disputes) plus one new upload (dispute evidence), both through the
worker. No infrastructure change.

## 1. Restated objective

Prompt 12's own deliverables are documents: `docs/BACKLOG.md` (the Phase 7 list with verified
endpoints, effort, dependencies and risk, and the "cheaper now than later" schema
recommendations for the owner's decision) and `docs/PROVIDER.md` with a test that holds the
provider boundary. The owner's amendments build three things on top: the contact log grows up
(edit by its author for 15 minutes, appended corrections afterwards, admin soft delete with a
reason shown struck through; a store or lease on every entry; promises that close automatically
when a qualifying payment settles on that lease; a "Previous promise" outcome of Kept, Rescheduled
or Broken instead of a checkbox); an **invoice builder** replaces "Create one-off charge" as the
entry point (several lines from the fee catalogue or free text, optional due date and memo, a
local draft with preview and PDF, Send creates one Stax invoice with all lines and e-mails it or
Charge now per 10d, each line a charge on the chosen lease so the ledger, grid and P&L stay
right, editable while draft, cancellable after sending); and a **read-only dispute list** fed by
Stax with evidence upload through the 10c documents path. Then the feature set freezes until
after Step 17.

## 2. Affected packages

`docs/` (BACKLOG, PROVIDER, DECISIONS for the schema decisions, RUNBOOK, DATA_OWNERSHIP),
`backend/ledger` (contacts, one-off charges, invoices, a new `invoice_builder`, disputes views),
`backend/payments` (provider interface `list_disputes`, `upload_dispute_evidence`; adapter and
fake; job kinds `disputes_sync`, `dispute_evidence_upload`; the invoice create path with several
lines; webhook dispute handler linking to the dispute row), `backend/tests`. New dependency for
PDF: **none** (the invoice PDF is rendered by the browser's print dialog from a print stylesheet;
see D7). Out of scope: everything in `docs/BACKLOG.md` §1.

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Documents first** (Prompt 12 proper): `docs/BACKLOG.md` and `docs/PROVIDER.md` written by Fable, the portability test by Fable; the two "cheaper now than later" items (S1 line-item kind and separate fee/surcharge columns; S2 operator contact consent columns) go to the owner as a decision in this plan's message; **S1 is assumed accepted for the builder's own table only if the owner says yes**; until then the builder writes line items without a `kind` and the plan carries the backfill note | Prompt 12; Checkpoint 12 |
| D2 | **Contact entry edit window and corrections** (owner 1): the author may edit `body`, `kind`, `happened_on`, `promised_date`, `promised_amount` within 15 minutes of `created_at`; after that only an **appended correction** (`corrections` rows: entry, author, body, created_at) shown under the entry; admin **soft delete** with a mandatory reason (`deleted_at`, `deleted_by`, `delete_reason`), shown struck through with the reason to billing and admin, hidden from viewers' CSV; a deleted promise counts for nothing. Every edit, correction and delete audited with before/after | Owner finding 1 |
| D3 | **Store or lease on an entry** (owner 2): `contact_entries.lease` FK (nullable; the store follows from the lease). Required when the operator has more than one active lease, preselected when exactly one, optional when none. Entries show on the operator page and on the store page (a Contact log section with **Add entry** that preselects that store's active lease). A promise is matched against **that lease's** open balance in the attention text ("on store 2477: owes 1,250.00") and, when the lease is blank, against the operator | Owner finding 2 |
| D4 | **Automatic close** (owner 3): a promise closes with status `kept` when a settled, non-refund payment with `amount >= promised_amount` settles **on that lease** (or on any of the operator's charges when the promise has no lease) with `settled_on` on or before the `promised_date` and after the entry was recorded; evaluated when a payment settles (transaction handler, manual payment) and by the daily tick for safety. A promise past its date that is still open becomes a broken-promise attention item (11b-a rule otherwise unchanged); it keeps status `open` until an outcome is recorded | Owner finding 3 |
| D5 | **Previous promise outcome** (owner 4): the entry form shows, when the operator has an open promise, a required choice **Kept / Rescheduled / Broken**: Kept closes it (`kept`, links the new entry); Rescheduled closes it as `rescheduled` and the new entry is a promise with the new date and amount (preselected from the old); Broken closes it as `broken` and the new entry records the contact. Statuses on the log: `open`, `kept`, `rescheduled`, `broken` (column `status`, default open; `closed_by` kept as the link). The "closes the promise" checkbox is removed. The attention list counts `open` promises past their date only | Owner finding 4 |
| D6 | **Invoice builder** (owner 5): "New invoice" on the store page (lease preselected: the store's active lease; a choice when several) and the operator page (lease chosen or operator-level). Table `invoice_drafts` (operator, lease nullable, memo, due_date nullable, status `draft`/`sent`/`cancelled`, created_by, timestamps) and `invoice_draft_lines` (draft, position, fee_type nullable, description, quantity, unit_amount, line_total; at least one line; amounts > 0; guarded text). A draft is **local only**: nothing goes to Stax. Preview page; **PDF** by the browser's print of a dedicated print layout (`/invoices/drafts/<id>/print/`); editable while draft (billing). **Send** (billing; `email`) or **Charge now** (admin; per 10d) converts the draft in one transaction into: one `one_off_charges` row per line (category from the fee type's code when it is a fee type, else `other`; `description` = the line text; price = line total; due date = the draft's or today + 15; collection `email`/`charge_now`; all on the chosen lease/operator), one local `Invoice` (our number, memo, due date) linked to the **first** line's charge and carrying every line in `invoice_line_items` (each line referencing its charge: `invoice_line_items.one_off_charge`), and one `invoice_create` job whose `Amounts` carries all lines; payments on the invoice allocate across its charges in line order (the companion mechanism of 11b generalised to "sibling charges of one invoice"); cancel after sending works as today and voids every sibling; the ledger, grid (one-off charges never appear in the grid; unchanged) and P&L count each charge by its category. The old "Create one-off charge" form is removed from the pages (its service stays as the single creation path the builder calls per line) | Owner finding 5 |
| D7 | **PDF without a dependency**: the preview and print pages carry a print stylesheet and a "Save as PDF" instruction; the owner's done-when "preview it" is met by the preview page and the print view. A server-side PDF library is a Phase 7 item if ever wanted | No new dependency during the freeze |
| D8 | **Dispute list** (amendment): `PaymentProvider.list_disputes(DisputeQuery)` → `DisputeRecord` (id, transaction id, reason, amount, status, respond_by, created, updated; statuses stored verbatim), `upload_dispute_evidence(dispute_id, name, data, content_type)` (`POST /file/dispute`, multipart, < 10 MB, jpeg/pdf/png/tiff; refused after respond_by with the provider's sentence); table `disputes` (provider id in `external_refs` as `dispute`, `payment` FK nullable, operator/store derived, reason, amount, status, respond_by, fetched_at, raw summary trimmed); job `disputes_sync` (worker: `GET /underwriting/disputes` for the last 90 days, paged; run by the daily tick and by **Refresh** on the page) and the dispute webhooks update the row; page `/disputes/` (billing and above; admin uploads evidence): list by respond-by, link to the payment and operator, days left highlighted under 7; **Evidence**: upload through the 10c documents path (the file is stored as a document on the dispute's operator with kind `dispute_evidence`, the same type/size/guard rules; **images are allowed only for this kind if the notice is acknowledged**: a dispute evidence image is a receipt or a signed lease page, never a bank form) and then sent to Stax by the job `dispute_evidence_upload` with the stored bytes; the result (`EVIDENCE_UPLOADED` / `UPLOAD_FAILED`) is re-fetched and shown; no accept/decline actions (Phase 7) | Owner amendment |
| D9 | **Roles**: contact edits by the author (billing), corrections by any billing, soft delete admin; builder draft/send billing, Charge now admin; disputes list billing, evidence upload admin | Pattern |

## 4. Interface contract

Models: `ContactEntry.lease|status|deleted_at|deleted_by|delete_reason`, `ContactCorrection`,
`InvoiceDraft`, `InvoiceDraftLine`, `InvoiceLineItem.one_off_charge`, `Dispute`, `DocumentKind.dispute_evidence`.
Services: `contacts.edit_entry|add_correction|delete_entry|record_outcome|close_kept_by_payment`,
`invoice_builder.{create_draft,update_draft,send,charge_now,cancel}`, `disputes.{sync,upload_evidence}`;
provider `list_disputes`, `upload_dispute_evidence`; jobs `disputes_sync`, `dispute_evidence_upload`.
URLs: `contacts:edit|correct|delete`, `stores:contacts|contacts_add`, `invoices:new_store|new_operator|draft|edit|preview|print|send|charge_now|cancel`,
`disputes:page|refresh|evidence`.

## 5. Work breakdown

| Batch | Task | Done when |
|---|---|---|
| A | D2–D5 contact log; tests | Edit within 15 minutes, refused after; correction appended; admin delete struck through; a promise on store 2477 appears on both pages and closes when a matching payment settles on that lease; outcomes recorded |
| B | D6–D7 invoice builder with sibling allocation and cancel; tests | A two-line draft on a store: preview, print view, send → one `invoice_create` with two lines, two charges on the lease, one local invoice with two line items; cancel voids both |
| C | D8 disputes; tests with the fake provider | The list shows fixture disputes with days left; Refresh queues the sync; evidence upload stores the document and queues the Stax upload; the status re-fetch shows `EVIDENCE_UPLOADED` |
| Docs (Fable) | BACKLOG, PROVIDER, portability test (done before the batches); DECISIONS (S1/S2 decisions as the owner rules), RUNBOOK §16, DATA_OWNERSHIP, README, QA-012 | |

A → B → C in sequence.

## 6. Test plan (before implementation)

Happy paths per decision; boundaries: the 15-minute window at 14:59 and 15:01 (clock pinned),
a promise with the payment exactly equal and on the promised date, a lease ended between the
promise and the payment, an operator with zero, one and two active leases, a draft with one line
and with 20 lines, quantity 0.5 and 3, line totals summing to the cent, a due date in the past
(refused) and absent (today + 15), a dispute with `respond_by` today, in 6 days, in 8 days, past;
evidence of 10 MB exactly and 10 MB + 1 byte, a .tiff, a .docx (refused), after respond_by
(provider 400 shown). Error paths: edit by another author → 403; delete by billing → 403; a
correction on a deleted entry → refused; send twice → one invoice; a line with a card number →
refused; cancel after a payment → refused; disputes sync with the provider down → the page keeps
the last fetch with its time. Adversarial (Fable): the invoice builder cannot create a line with a
negative, zero or non-cent amount or total mismatch; a draft of operator A cannot be sent against
operator B's lease; sending a draft whose lease ended is allowed for arrears and refused for
autopay; a viewer cannot delete, edit or send anything; the struck-through entry's reason is not
shown to viewers; the dispute evidence path never bypasses the 10c guard (a PDF with a routing and
account pair is refused before any Stax call); the dispute page shows no unmasked transaction id;
the portability test (already written) stays green after the new provider methods.

## 7. Risk register

- **Two Stax calls added** (list disputes, upload evidence): both through the worker, the
  limiter and the breaker; the daily sync is one paged read; uploads are staff actions.
- **Money state**: sibling allocation across several charges of one invoice is new; the
  property test of open balances gains multi-line invoices; payments on a multi-line invoice
  allocate in line order and are audited per charge.
- **Freeze**: nothing from BACKLOG §1 starts; the plan's three builds are the owner's amendments.
- **Schema decisions S1/S2** are the owner's; the builder's `invoice_line_items.one_off_charge`
  column is needed regardless (it is the allocation key).
- Irreversible: none.

## 8. Acceptance criteria

Gate green on both engines; `docs/BACKLOG.md` and `docs/PROVIDER.md` published; the S1/S2
decision recorded in `docs/DECISIONS.md` once the owner answers; on staging: a two-line draft on
store 2476 previewed, sent, one Stax invoice with two lines and two charges on the lease; a
promise on store 2477 shows on both pages and closes on a matching payment; an entry edited
within 15 minutes and refused after; a viewer cannot delete; the dispute list renders (empty on
the sandbox until a dispute exists).

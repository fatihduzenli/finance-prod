# PLAN-008a — send_invoice object ID, manual invoice linking, auto-link on create_invoice

**Date:** 2026-10-02 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Origin:** owner findings from the Checkpoint 8 and 9 clicks (2026-10-02).

## 1. Facts observed on staging (stored redacted bodies, keys only)

- `create_invoice`: event name in the header `stax-event-name`; body is the bare invoice
  object: `id`, `customer_id`, `schedule_id`, `payment_method_id`, `total` (integer dollars in
  this instance), `total_paid`, `balance_due`, `invoice_date_at`, `due_at` (null present),
  `created_at`, `updated_at`, `url`, `meta` (`memo`, `internalMemo`, `subtotal`, `tax`,
  `lineItems[]`, payment-method toggles), `payment_attempt_failed`, `payment_attempt_message`,
  `is_partial_payment_enabled`, `is_merchant_present`. **No `sent_at`, `viewed_at`, `paid_at`
  key exists on the object.** Closes TODO-VERIFY-015 (header), 042 (bare object for invoices),
  narrows 044 (keys `total_paid`, `balance_due`; no timestamp keys).
- `send_invoice`: body is an email envelope: `toEmail`, `toName`, `ccEmails`, `bccEmails`,
  `mergeVars.user.*`, `mergeVars.invoice.*` with the invoice object under `mergeVars.invoice`
  (`id`, `url`, `meta`, …). No top-level `id`.

## 2. Rules

1. **Object ID extraction** (`payments/webhooks/receiver.py`): top-level `id` first; else, in
   order, `mergeVars.invoice.id`, `invoice.id`, `transaction.id`, `data.id`, `batch_id`. The
   path used is recorded as `object_id_source` (new column) and shown on the event page. Only a
   plain identifier is accepted, as today.
2. **`send_invoice` handler:** re-fetch the invoice by that ID; apply the provider state through
   the sync writer; if the re-fetched status is `SENT`, `VIEWED` or `PAID` and our `sent_at` is
   null, set `sent_at` to the event's `received_at` (the invoice object carries no such
   timestamp; recorded in `docs/DATA_OWNERSHIP.md`: `sent_at` is derived from the first send
   event). `viewed_at` likewise from the first `update_invoice` whose re-fetched status is
   `VIEWED`; `paid_at` from the first re-fetch showing `PAID`. Never from the body.
3. **Auto-link on `create_invoice`** (and on `update_invoice` when the local invoice has no
   charge): after the re-fetch, if the invoice has no `schedule_id` and exactly one open rent
   charge of that operator (status `due`, `unpaid` or `partial`, balance > 0, no invoice yet)
   has `amount_due` equal to the invoice total **and** `invoice_date_at` inside the charge's
   period month, link it (invoice `rent_charge` set, audit `invoice.auto_linked`). Otherwise
   leave it unlinked and record the outcome `processed: unlinked (n candidates)`.
4. **Manual link** on the charge page: "Link invoice" (admin or billing) lists the operator's
   local invoices with no charge: number, provider date, total, provider status, masked
   provider ID. Choosing one links it; if the total differs from the charge **balance**, the
   form requires the exact-value confirmation checkbox and a mandatory note, and the charge is
   flagged with "Linked invoice total differs from the balance". Audit `invoice.linked` with
   actor, masked ID, totals and the note. One charge, one invoice: linking a second invoice to
   a charge that has one is refused. Unlinking is not offered.
5. **Match invoices** on the run page keeps reading schedules only; the page text says so and
   points to the manual link.
6. `docs/TODO-VERIFY.md`: 015 closed, 042 closed for invoices (transactions still open), 044
   rewritten, new 057: whether `total` arrives as an integer or a decimal string on other
   invoices (observed integer dollars once).

## 3. Tests

| # | Case | Author |
|---|---|---|
| 1 | Receiver: `send_invoice` envelope → object ID from `mergeVars.invoice.id`, source recorded; a body with both top-level and nested IDs uses the top-level one; a nested non-identifier is rejected | Opus |
| 2 | `send_invoice` handler: re-fetch; `sent_at` set once from the event time when status is SENT; a second send event does not move it; body status never read | Opus |
| 3 | Auto-link: exactly one matching open charge → linked and audited; zero or two candidates → unlinked with the count; a schedule invoice is never auto-linked here; total or period mismatch → not linked | Opus |
| 4 | Manual link: list shows only the operator's unlinked invoices; equal total links without confirmation; different total needs checkbox and note, flags the charge; second invoice refused; viewer 403; audit row | Opus |
| 5 | Adversarial: an invoice of another operator offered by ID in the POST (refused); a charge of another operator; concurrent manual link of the same invoice to two charges (PostgreSQL); the note with a card number; confirmation value tampering | Fable |
| 6 | Whole suite both engines, lint, format, types, migrations | Fable |

## 4. Acceptance

Gate green; on staging: the stored `send_invoice` event replays to `processed` with `sent_at`
set; the $1,500 dashboard invoice links to the October charge from the charge page; the wire
payment then marks it paid in Stax.

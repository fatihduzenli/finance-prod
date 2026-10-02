# PLAN-009 — Payments ledger, external payments marked in Stax, refunds and voids

**Date:** 2026-10-02 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Inputs:** Prompt 9 and the owner's amendments (2026-10-02, which win on conflict); `docs/DATA_OWNERSHIP.md`;
`docs/SCOPE.md` §3 Flow 2, §9 Round 3; `docs/inputs/STAX_ENDPOINTS_2026-09-30.md`; the specification
pages read on 2026-10-02: mark-an-invoice-paid-in-cash-or-check (`POST /invoice/{id}/pay/{method}`,
`method` enum `cash|check|giftcard`, query `apply_balance` integer "for a partial payment, less than
the invoice total", `email_receipt`, `check_number` max 25, `payment_note` max 255; no body),
refund-transaction (`POST /transaction/{id}/refund`, body `total` string; partial refunds allowed up to
the remaining refundable amount; 422 above it), void-transaction (`POST /transaction/{id}/void`; whole
amount only).

## 1. Restated objective

One ledger for every payment. A manual payment (wire, Zelle, other) recorded against a charge
that has a Stax invoice is also marked paid in Stax through the pay-as-cash endpoint, with a memo
that says how and when it was really paid, after a check that Stax does not already show it paid,
and followed by a re-fetch. The ledger lists every payment with filters, totals and a CSV export,
and shows pending ACH as pending. A short payment leaves its charge partial and the balance is
shown carried into the next period. Refunds and voids are admin-only, confirmed, performed in
Stax, re-fetched, audited, and modelled as Stax models them. No cash or check method exists in
this app's UI.

## 2. Stop conditions, answered

| Condition | Answer |
|---|---|
| Cash/check endpoint cannot record a check number or reference | It can: `check_number` (25) and `payment_note` (255). Our reference goes in `check_number` when it fits, the memo in `payment_note`. |
| Partial refunds behave differently from the spec when tested | Cannot be tested: blocked on Stax ACH. Recorded. |
| Endpoint or field unclear (amendment 1) | **One point is unclear and is handled, not stopped on:** `apply_balance` for a partial manual payment is an integer with no stated unit (dollars or cents). A **partial** manual payment is therefore recorded locally and **not** sent to Stax; the charge is flagged "Stax invoice not updated: partial amount" until TODO-VERIFY-028 is settled in the sandbox. A **full** payment is marked in Stax without `apply_balance`. |

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Marking in Stax, full payments only:** `record_manual_payment` writes the local payment first; if the charge has an invoice with a Stax reference, the service then re-fetches the invoice; if its status is already `PAID`, it records `payment.provider_already_paid` in the audit log, flags the charge, and makes no further call; otherwise it calls `mark_invoice_paid_cash_or_check(method="cash", payment_note="Paid by <wire|Zelle|other> on <MM/DD/YYYY>. <staff note>" cut to 255, check_number=<reference, ≤25, letters/digits/hyphen only>, email_receipt=False)`, then re-fetches the invoice and records its state through the sync writer. Our ledger keeps `method = wire|zelle|other`; `cash` exists only on the Stax side because Stax offers no wire or Zelle method and this is what staff do by hand today (owner Q19). Documented in `docs/DATA_OWNERSHIP.md` and `docs/RUNBOOK.md` | Amendment 1 |
| D2 | A Stax failure during marking leaves the local payment in place (money was received), flags the charge `needs_review` with the failure class and field names, and offers a "Retry marking in Stax" button (billing, 60-second lock as for operators). No automatic retry | Local first; no loops |
| D3 | **Partial manual payments** are local only until TODO-VERIFY-028 (see §2); the charge shows the flag | Spec ambiguity on `apply_balance` |
| D4 | **Ledger** `/payments/`: filters date range (paid-on or initiated), method, operator, store, status (`pending`, `settled`, `returned`, `voided`, `refunded` where `refunded` means refund rows); 25 per page; totals row for the filtered set: settled total, pending total (shown separately, never added to collected), returned total, refunded total; **CSV export** of the same filter at `/payments/export.csv` (any verified role; UTF-8; one row per payment; amounts as plain decimals; no secrets, no masked-ID unmasking); note indicator where the charge has a charge note | Amendment 2, 5 |
| D5 | **Carry-forward:** `leases_due` and the run page show, per lease, the sum of open balances of earlier charges as "Carried balance"; `create_charges` does not change the new charge's amount; the charge page shows due, paid, balance. Collection of a carried balance is a `charge_adjustments` one-off invoice in a later prompt | Owner rule E; adjustments are separate |
| D6 | **Refund** (admin): confirmation page with operator, store, invoice number, original payment amount, remaining refundable amount, refund amount (full by default, partial allowed between 0.01 and remaining), a typed confirmation checkbox exactly as in PLAN-007c; on confirm: `refund_transaction(id, amount)`, re-fetch the transaction and the invoice, create a new payment row `is_refund=True`, `parent_payment`, amount, status from the re-fetch, recompute the charge (reopens to `partial` or `unpaid`), audit `payment.refunded` naming the actor. **Void** (admin): same page shape, whole amount only; `void_transaction(id)`, re-fetch both; **the original payment becomes `voided`** (no new row: a void is not a new transaction at Stax, and the webhook handler of Prompt 8 already models it this way; a new row would double count); charge recomputed; audit `payment.voided`. Amendment 4 says voids are new rows; this plan deviates for the stated reason and asks the owner to confirm | Prompt 9 items 4, 5; consistency with Prompt 8 |
| D7 | Refund and void buttons render only for admin and the routes refuse POST below admin (registry); they exist only for `settled` ACH payments with a Stax transaction reference; a refund of a returned or voided payment is refused | Checkpoint 9 |
| D8 | The payment detail page `/payments/<id>/` shows the chain: parent, refunds, invoice, charge, audit rows | For the confirmation and the matrix |
| D9 | Every list (`/leases/`, `/payments/`, `/charges/` where it exists) gains an operator filter | Amendment 5 |

## 4. Scenario matrix (tests, FakeProvider; sandbox proof as stated)

| # | Scenario | Expected | Sandbox proof |
|---|---|---|---|
| 1 | Full manual payment on an invoiced charge | local payment settled; Stax re-fetched before and after; marked `cash` with the memo; invoice `PAID` via sync writer; charge paid | **provable now** (dashboard invoice) |
| 2 | Partial manual payment | local payment; charge partial with balance; no Stax call; flag | provable now |
| 3 | Two partials completing the charge | charge paid; second payment triggers no Stax mark (both partial, D3) and the flag stays until TODO-028 | provable now |
| 4 | Full ACH settlement then partial refund | refund row linked, charge partial, invoice re-fetched | blocked on ACH |
| 5 | Refund of a partial ACH payment | as 4 with smaller amounts | blocked on ACH |
| 6 | Void of a pending ACH payment | original `voided`, charge reopened, invoice re-fetched | blocked on ACH |
| 7 | Manual payment for an invoice Stax already shows paid | local payment kept, mismatch audited, no second mark | provable now |
| 8 | Pay half, refund the half, pay in full (Checkpoint 9) | ends paid with one settled, one refund, one settled row; balance 0 | partly blocked |
| 9 | Refund above the refundable amount | refused locally before any call | unit |
| 10 | Billing user attempts refund or void | 403, no call | unit |

## 5. Work breakdown

| Batch | Task |
|---|---|
| A | Marking in Stax inside `record_manual_payment` (D1 to D3), retry button, provider mapping for the mark-paid query parameters (method enum, `check_number`, `payment_note`), `InvoiceRecord` fields already present; ledger page, CSV export, filters, totals (D4); carry-forward display (D5); operator filters (D9); tests for matrix rows 1, 2, 3, 7 and the ledger |
| B | Refund and void (D6 to D8): pages, services, provider calls, re-fetch, audit; payment detail; tests for rows 4, 5, 6, 8, 9, 10 |
| C | README rows; `docs/` by Fable (DATA_OWNERSHIP, RUNBOOK, TODO-VERIFY-016 closed, 028 kept) |

## 6. Test plan additions by Fable (adversarial)

Memo text with a card number (the memo is built from the staff note, which the guard already
refuses); reference longer than 25 or with spaces; a manual payment recorded twice quickly
(replay key); Stax mark succeeding but the re-fetch failing (local state, flag, no second mark on
retry because the pre-check sees PAID); refund amount with three decimals, negative, equal to
remaining, remaining plus one cent; two admins confirming the same refund at once (PostgreSQL);
CSV export with a note containing commas, quotes and newlines (must stay one row per payment and
parse back); export of 5,000 payments under two seconds; viewer reaching every refund route.

## 7. Acceptance

Gate green on both engines; migrations clean; every matrix row has a passing test; on staging:
the owner's four clicks in the amendment's "done when".

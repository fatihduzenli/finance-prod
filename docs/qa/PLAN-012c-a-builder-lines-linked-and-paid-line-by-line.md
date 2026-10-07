# PLAN-012c-a — Every builder line is linked to its invoice; a multi-line invoice is paid line by line

**Date:** 2026-10-07 · **Origin:** owner's findings 3 and 4 of Prompt 12 on staging, in writing,
on invoice INV-2026-0006 (store 2476, two lines: Damage Repair $300 + late fee $50):
*"(3) Only line 1 of a multi-line invoice is linked to the invoice. The 'late fee' charge page
shows 'No invoice linked', has no 'Record a payment' action, and offers only 'Waive' (with the
text 'closes a charge that has no Stax invoice'). Every line created by the invoice builder must
be linked to the invoice, and Waive must be refused on it while the Stax invoice is live, as
already done for other charges. Also repair the existing staging data: link the late fee line to
INV-2026-0006. (4) I recorded a $300 wire payment on the Damage Repair line; it shows 'Needs
review – Provider invoice not updated: partial amount' because the Stax invoice is $350. The flag
is fine. Confirm that recording the remaining $50 marks the invoice paid-as-cash at Stax with the
memo and clears the flag, and add a test for a multi-line invoice paid line by line.
(Invoice-level payment entry will come with Step 12d, not now.)"*
**Lead/QA:** Fable · **Implementer:** Opus · **Ships with:** 012a, 012b, 012c, one push.

## 1. Restated objective

Batch B modelled a builder invoice as "the first line's invoice": `invoices.one_off_charge` names
line 1 and `invoice_line_items.one_off_charge` names every line, and later lines were "paid
through the first line" (no Record a payment, no invoice shown, Waive offered as if no invoice
existed). The owner wants every line to behave as a charge billed on that invoice: its page shows
the invoice and the provider status, offers **Record a payment**, refuses **Waive** while the Stax
invoice is live, and refuses Link invoice; and a manual payment on any line marks the Stax invoice
paid as cash, with the memo, once the lines' payments together cover the invoice total, clearing
the partial-amount flag on every line. Invoice-level payment entry is Step 12d and not built here.

## 2. Affected packages

`backend/ledger` (`services/one_off_charges.py`, `services/rent_run.py` where it resolves a
charge's invoice, `views/one_off_charges.py`, the charge detail template), `backend/tests`. No
schema change is expected: the line-item link already exists on every builder line, so the
"repair" of INV-2026-0006 is the code path reading it (D5). Out of scope: the allocation of a
payment across lines (PLAN-012 D6 stays), the builder's pages, Step 12d.

## 3. Decisions

- **D1 — a charge's invoices.** `one_off_charges.invoices_of(charge)` is the queryset of invoices
  that either name the charge (`one_off_charge=charge`) or carry a line item naming it
  (`line_items__one_off_charge=charge`), distinct, by issue date and number. Every place that
  today reads `charge.invoices` for a one-off charge uses it: the detail page's invoice table and
  `can_link_invoice`, `rent_run.provider_invoice` (for a `OneOffCharge`; rent and insurance
  charges unchanged), `live_provider_invoice`, the cancel refusal, the delete-invoice remedy,
  the waived-with-live-invoice dashboard query, the review and statement rows that look up a
  charge's invoice. `is_later_line` stays for allocation and wording only.
- **D2 — Record a payment on any line.** `can_record` no longer excludes later lines. A manual
  payment on a later line is recorded on that charge (no overflow to siblings; the first-line
  overflow rule of D6 is unchanged). After any manual payment on a charge billed on a builder
  invoice, the invoice is **covered** when the settled, non-refund manual and provider payments
  across all of its lines' charges reach `invoice.total` (`paid_in` summed over the invoice's
  charges, waived and voided lines excluded from the total as `billed_total` does). Covered →
  the `one_off_payment_mark` job is queued for this charge and payment and marks the invoice paid
  as cash or check at the provider with the memo (the existing rules; the memo names the method,
  the reference and the paying charge); on success the review flag of **every** charge on the
  invoice is cleared and each is audited. Not covered → the paying charge gets the partial-amount
  flag as today (`offline_marking.PARTIAL_AMOUNT`), the other lines untouched. A single payment
  on line 1 equal to the invoice total keeps today's behaviour (whole, marked at once).
- **D3 — Waive and Link invoice.** The page's `waive_refusal` and `can_link_invoice` read the
  charge's invoices through D1, so a later line with a live Stax invoice shows the refusal
  sentence and no Link invoice; the service already refuses (PLAN-012 D6) and keeps doing so.
- **D4 — the mark path.** `rent_run.mark_charge_invoice(ONE_OFF, charge_id, payment_id)` and
  `retry_charge_mark` resolve the invoice through D1 so a later line's mark finds the invoice;
  the retry lock and failure handling are unchanged; the marking amount is the invoice total
  (the provider call is "paid in cash", not an amount).
- **D5 — the staging data.** Every builder line already has an `invoice_line_items` row naming
  its charge (`_convert` writes them), so INV-2026-0006's late fee is linked once D1 ships; no
  data migration. Opus adds a management command `invoice_line_links --check` (read-only) that
  lists builder invoices whose line items lack a charge or whose charges lack a line item, so the
  owner's repair can be verified on staging without a database query; if it lists anything, a
  data fix is a separate, owner-approved step.

## 4. Interface contract

`one_off_charges.invoices_of(charge) -> QuerySet[Invoice]`; `one_off_charges.invoice_covered(invoice)
-> bool` (D2); `rent_run.provider_invoice` accepts a one-off charge billed as a later line; the
management command `invoice_line_links` with `--check` (exit 1 when it lists anything). No route,
template structure, schema, environment or provider change. The audit action for the cleared
flags is the existing marked action with the sibling named.

## 5. Work breakdown (one batch, Opus)

| Task | Files | Done when |
|---|---|---|
| D1 resolver and its call sites | `services/one_off_charges.py`, `services/rent_run.py`, `views/one_off_charges.py`, `templates/ledger/one_off_charges/detail.html` | a later line's page shows the invoice, provider status, Record a payment, the Waive refusal, no Link invoice |
| D2, D4 coverage and marking | `services/one_off_charges.py`, `services/rent_run.py` | the owner's scenario: $300 on line 1 → partial flag; $50 on line 2 → one mark call with the memo, both flags cleared, invoice paid |
| D5 command | `ledger/management/commands/invoice_line_links.py` | `--check` exit 0 on clean data, 1 with a listing |
| Tests | `tests/test_invoice_builder.py`, `tests/test_one_off_charges.py` | §6 |

## 6. Test plan (before implementation)

Happy: the owner's scenario end to end through the pages and the worker (fake provider): line
1 $300 wire → `PARTIAL_AMOUNT` on line 1, no provider call; line 2 $50 wire → one
`mark_invoice_paid_cash_or_check` call with the memo, flags cleared on both, invoice
`provider_status` as the fake returns, both charges paid, the ledger and the operator balance
at zero; the reverse order (line 2 first); a three-line invoice paid in three payments; a single
$350 payment on line 1 (today's whole-payment path unchanged, line 2 allocated). Boundaries:
$299.99 then $50.01 (covered to the cent); a waived later line reduces the coverage target; a
payment on a later line larger than its price is refused or flagged as today's rules say (state
which). Error paths: Waive on the later line while the Stax invoice is live → refused with the
sentence, on the page and in the service; Link invoice absent on both lines; the mark job's
provider failure → the retry path on the paying line; `invoice_line_links --check` on a fixture
with one orphan line item exits 1 and names the invoice. Adversarial (Fable): a later line of
operator A cannot be paid from operator B's charge page; marking is never queued twice for one
covered invoice when two lines are paid in quick succession; a refund on line 1 after coverage
does not un-mark anything (Stax owns it) but flags the charge for review; the command never
writes; a rent charge's invoice resolution is byte-for-byte unchanged.

## 7. Risk register

- Money state: coverage is computed from settled payments only; pending payments never mark.
- The mark is one provider call per covered invoice; the job is keyed on the paying charge and
  payment as today, and D2 queues it once.
- Irreversible: a mark at the provider cannot be undone by this app (as today); the coverage
  rule is strict equality or more, never "close enough".

## 8. Acceptance criteria

Gate green on both engines; on staging, the late fee page of INV-2026-0006 shows the invoice and
Record a payment, Waive is refused, and recording the $50 marks the invoice paid at Stax with
the memo and clears the flag on both lines; `invoice_line_links --check` on staging exits 0.

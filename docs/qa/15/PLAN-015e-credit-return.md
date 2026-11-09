# PLAN-015e — a returned bank debit that was kept as a credit reverses the credit and alerts staff

**Owner's request (2026-11-08): "when a bank debit that was kept as credit is later returned by the bank, the
credit currently stays. That must not happen. The credit must be reversed (or the balance must show as owed
again) and staff must be notified."** Plan by Fable; implementation by Opus; gate and sign-off by Fable. Gate class
full (a migration, the return path, money). Record: `docs/qa/15/QA-015.md`.

## 1. Objective

A bank-debit payment that staff kept as a LeaseCredit (PLAN-015d "Keep as credit": the payment row re-pointed to the
credit, or split into a held row) can be returned by the bank days later. Today the webhook's return path finds no
charge on the row and changes nothing else: the credit stays, its applications stay, and in the split case the held
row is never even found. After this plan: the return reaches the credit; an unapplied credit is reversed to zero; an
applied credit has its applications returned, so the charges they paid are owed again; the lease is flagged Needs
review with its own reason; the dashboard's Needs attention lists it until staff clear the flag; the managers get an
internal e-mail; the NSF fee attaches to the right lease; and the ledger, the P&L footer and the LEASE card agree.

## 2. Scope

**In:** `backend/payments/webhooks/handlers/transaction.py` (the return reaches a credit row and its split
siblings), `backend/ledger/services/credits.py` (`reverse_from_return`), `services/nsf.py` (the held-row branch),
`services/attention.py` (the item), `services/messaging.py` + a seeded internal template, `services/start_date.py`
(the split rows linked), `models.py` + one migration (`LeaseCredit.reversed_at`, `reversed_reason`;
`Payment.split_from`; `ReviewReason.CREDIT_RETURNED`), the LEASE card's Credit row and the payments ledger words, the
timeline (the `status` kind lists the reversal), the tests.

**Out:** the processor (Rule 2: the app writes nothing there; the return is read from the re-fetch as today); a
re-debit of a reversed credit (no automatic re-debit: the owed charges follow the normal collection path, §8);
credits recorded at payment entry by wire, Zelle, check or cash (they cannot return); refunds.

## 3. Interface contract

- **Finding every row of the returned transaction.** `Payment.split_from` (nullable FK to Payment, set by
  `start_date._hold` on the held row it splits off; the whole-row case needs none). `transaction._transition` applies
  RETURNED to the row the ExternalRef names **and to every row whose `split_from` is that row** (same `returned_at`,
  code and reason; each audited `payment.status_changed`). Existing data: a held row split before this plan has no
  link; a data step in the migration sets `split_from` from the `payment.held_as_credit` audit rows (`held_payment` →
  the original) where both rows exist.
- **The credit branch.** `_refresh_charge`: a row with `credit_id` and no charge → `credits.reverse_from_return(
  payment)`; no silent fall-through remains (a row with neither a charge nor a credit logs a warning and is audited
  `payment.return_unplaced`).
- **`credits.reverse_from_return(payment) -> CreditReversal`** (`credit`, `unapplied: Decimal`, `applications:
  list[Payment]`): under the credit's row lock — the credit's `reversed_at = now`, `reversed_reason = the return code
  and reason`, `remaining = 0`; every application (a `method=credit` payment with `credit=` this credit, SETTLED) is
  set RETURNED with the same code and reason, audited `payment.status_changed`, and its charge recomputed
  (`rent_run.recompute_charge` / `one_off_charges.recompute`) so it reads owed again with `needs_review`; audited
  `lease_credit.reversed` (`after`: amount, unapplied, applications' ids and amounts, the payment, the reason).
  Idempotent: a reversed credit reverses nothing again. A credit already refunded at lease end (`refund_all`) is
  recorded as `reversed_after_refund` and flagged only (the money went out; staff decide).
- **Staff.** (1) `nsf._flag_lease(lease, ReviewReason.CREDIT_RETURNED, …)` on the credit's lease (never
  `first_active_lease`). (2) `attention`: a new kind `credit_returned`, severity HIGH, driven by the
  `lease_credit.reversed` audit rows of leases still flagged, worded "Credit of $X reversed: the bank debit of <date>
  was returned (<reason>); <N> charges owed again" with the store link; it clears when the lease's Needs review is
  cleared. (3) An internal e-mail to `CompanySettings.manager_emails`: a seeded `credit_returned` template
  (`internal=True`, the Outbox holds it while mail is off), placeholders amount, store, lease, return reason, the
  charges owed again; no account number.
- **NSF.** `nsf.on_return` for a credit row: the lease is `payment.credit.lease`; the NSF fee attaches to it; **no
  automatic re-debit** (`redebit_sources` has nothing to pay; the owed charges follow the normal collection path and
  reminders B–E); no template F to the operator (there is no charge for its words — §8 Q1); `payment.return_processed`
  records `credit_reversed` and the fee. **The operator is told** (owner, 2026-11-08): a seeded template `credit_returned_operator`
  (`internal=False`, to the lease's billing e-mail, held while mail is off) with the owner's wording: "Your bank
  returned your payment of {amount} made on {debit_date} (reason: {return_reason}). It had been kept as a credit on
  store #{store_number} {store_name}; that credit is reversed and your balance is now {balance}. Please contact us at
  {support_email}." — `balance` is the store's open balance after the reversal; placeholders added to
  `ledger/merge.py`'s allow-lists; no account number anywhere.
- **Figures.** `credits.held()` and `balance()` exclude reversed credits; the LEASE card's Credit row reads $0.00 and
  "Credit of $X reversed <date>" while flagged; the payments ledger shows the returned application rows as returns;
  `receivables.open_balances` needs no change (the recomputed charges are owed).
- **Split-amount fee.** The NSF fee and any future re-debit of a split transaction use the transaction's total (the
  sum of the row and its `split_from` siblings), not the reduced original.

## 4. Work breakdown (Opus; branch `fix/credit-return` from main; a commit per task)

1. Migration + model fields + the data step linking existing split rows; `start_date._hold` sets `split_from`;
   `FROZEN_CHOICES`/guard lists; tests (forward/back, the data step, the shape constraints).
2. `transaction._transition` returns the siblings; `_refresh_charge`'s credit branch; `credits.reverse_from_return`
   with the application reversal; tests through the webhook helpers (`provider_txn` + a `returned` transaction):
   whole-row case, split case, unapplied, partly applied, fully applied, already refunded, replayed webhook.
3. `nsf.on_return` for a credit row (the lease, the fee, no re-debit, no F, the audit); the split-total fee; tests.
4. Staff and the operator: the ReviewReason, the attention kind, the internal template and message, the operator's
   template and message (§3); the LEASE card and ledger words; the timeline entry; tests (the item appears and
   clears; both e-mails' bodies; the Outbox holds them; no account number).
5. Self-review and report.

## 5. Test plan

§4's, plus Fable's adversarial set: a return on a credit that was applied to two charges on two months (both owed
again, both flagged); a return after the lease ended and the credit was refunded (flag only, no negative credit);
the webhook replayed twice (one reversal, one e-mail); a split row whose original was already returned before the
migration (the data step links it; a later replay returns the held row too); the attention item is HIGH and names
no account number; a non-ACH credit (wire) is never "returned" (no path); Rule 2: the FakeProvider sees no call.

## 6. Risk register

- **Money state:** the credit's reversal and the applications' returns are local rows only (Rule 2); no processor
  call. The owed-again charges go through the normal collection path.
- **Existing data:** the data step covers held rows already split; a return before this plan ships would have been
  missed — the QA entry asks staff to review open credits from PLAN-015d's "Keep as credit" (none exist on staging
  yet).
- **Migration** on payments/credits tables; reversible; refuses to reverse while a reversed credit exists.

## 7. Gate class and acceptance criteria

Full gate. Accepted when §5 is green on both databases, the PR's CI is green, the migration applies and reverses,
and the QA-015 entry names the counts and the verdict.

## 8. Owner's decisions (2026-11-08)

1. Approved as drafted, top priority before further design work; full gate; merge on green; verify staging.
2. The operator is told, with the owner's wording (§3, the `credit_returned_operator` template).

## 9. Size

Medium: one Opus session (about 4 hours of build and targeted tests), one full gate (about 3 hours), one review
round expected. One migration, five service modules, one template, one e-mail template, about 20 new tests.

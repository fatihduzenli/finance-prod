# PLAN-015d — Change start date, free rent by days, deposit timing

**Owner's request (2026-11-07), queued after PLAN-015c under the same approval; gate class full
(migrations, money paths, the processor).** Plan by Fable (draft; §9 lists what the owner decides
before Opus starts); implementation by Opus; gate and sign-off by Fable. Record: `docs/qa/15/QA-015.md`.

## 1. Objective

Three lease features. (1) **Change start date** on a prospective lease, or an active lease in its
first month: billing role, a required reason, the effects shown before confirming — an unsent move-in
draft recalculates; a sent, unpaid move-in invoice is voided and a new draft prepared; a paid one is
kept as a credit or refunded (first-month rent only; the deposit kept); rent charges between the old
and new start are voided; autopay, reminders and calendar items follow the new date; audited and in
Notes & follow-ups. (2) **Free rent by days** on New lease and Edit lease: Days and Starts (from the
move-in date, or from the 1st of the following month); rent charges keep their amount and carry a
concession netting the free days ("Free rent per lease agreement (<from> – <to>)"), split by days for
a month partly inside the period; concessions reported apart (P&L billed, concessions, net; the
Revenue report's Concessions column); shown on the LEASE card and as an invoice line. (3) **Deposit
timing** on New lease: with the move-in invoice (default), due on a date (its own charge), or in
installments (2–12, first date, cadence) through the installment-plan engine; the LEASE card shows
the progress; reminders follow the deposit's own due dates; lease-end and transfer use only the
deposit collected; Change start date moves the deposit schedule only when ticked.

## 2. Scope

**In:** `backend/ledger/` — `models.py` + migrations (below), `services/move_in.py` (rebuild),
`services/start_date.py` (new: the change's preview and apply), `services/free_rent.py` (new),
`services/deposits.py` + `services/installments.py` (the deposit plan), `services/rent_run.py`
(concessions on the months it creates; void), `services/invoice_builder.py` (the concession line;
void of a rent-alone invoice), `payments/one_off_invoices.py` (the provider void), `services/pnl.py`,
`services/reports.py` + the two report templates, `services/receivables.py` + `services/reminders.py`
(deposit due dates), `services/leases.py` (`move_autopay` generalised to a new start), `forms.py`,
`views/leases.py`, the New lease and Edit lease templates, the LEASE card, `urls.py`, the registry,
Notes & follow-ups (`timeline` kind `status` gains the start-date change), and the tests.

**Out:** the portal (reads the same invoices); the processor adapter beyond the void and the
discount line it already supports (§9 Q2 decides); insurance; the transfer wizard's own deposit flow
beyond reading the collected amount; the lease-end wizard beyond the same.

## 3. Interface contract

**Data (one migration per model group, all reversible):**
- `RentChargeStatus.VOID` — a charge that never fell due (a start moved past it); `balance` 0 (the
  generated column takes `amount_due − amount_paid − concession` with void charges excluded by the
  services the way `waived` is); P&L and the Revenue report treat void as "not a bill" (as waived);
  the Charges tab shows "Void" (grey).
- `RentCharge.concession` (Decimal, default 0, `0 ≤ concession ≤ amount_due`) and
  `concession_text` ("Free rent per lease agreement (Dec 10 – Jan 8)"); `balance` becomes
  `amount_due − concession − amount_paid`.
- `Lease.free_rent_days` (0–366, default 0) and `Lease.free_rent_from` (`move_in` / `next_month`).
- `InvoiceLineKind.DISCOUNT` used: a positive `line_total` subtracted in the invoice's totals; the
  provider body carries it the way the processor accepts a discount (§9 Q2).
- `OneOffCharge.due_date` already exists for the deposit; a deposit plan is an `InstallmentPlan`
  with `origin = deposit` (new value) and `collection` as the lease's channel; `installments`'s
  deposit refusal lifts only for `origin = deposit`.
- Audit actions: `lease.start_date_changed` (before/after the snapshot; `after["reason"]`,
  `old_start`, `new_start`, `invoice_outcome` ∈ draft_rebuilt / voided_and_redrafted / kept_as_credit
  / refunded, `deposit_moved`), `rent_charge.voided`, `lease.free_rent_set`.

**(1) Change start date** (`services/start_date.py`):
- `preview(lease, new_start: date, *, today) -> StartDateChange`: refused unless prospective, or
  active with `today` inside the start month; refused when `new_start` is inside a billed month of
  another lease of the store (overlap) or more than 12 months away; lists the effects: the draft
  state (`none` / `unsent` / `sent_unpaid` / `paid`), the rent charges to void (every charge with
  `period_month` between the old and new start months that is not paid), the first-month amount at
  the new date (`rent_run.first_month`), autopay (whether live schedules move), the deposit (its due
  date or plan, and whether it would move), the credit or refund amount when paid.
- `apply(lease_id, new_start, *, reason, choice: credit | refund | None, move_deposit: bool,
  actor, today) -> StartDateChange`: under the row lock — `started_on` and the only rent row's
  `effective_from` move (as `activate`); charges voided (`rent_charge.voided`); the draft: unsent →
  `move_in.rebuild_draft` (a new one-time key; the old draft deleted); sent unpaid → the invoice
  voided at the provider through a new `invoice_void` job (`payments/one_off_invoices.void`, which
  generalises `delete` to a rent-alone invoice) and a new draft prepared; paid → `choice` required:
  `credit` creates a `LeaseCredit` of the rent paid (the deposit's payment untouched) applied to the
  new first month's charges when they exist (`credits.apply`), `refund` queues `PAYMENT_REFUND` for
  the rent part through the existing refund path (admin confirmation stays the refund path's), and
  the invoice is voided and redrafted for the deposit-only remainder when needed; autopay: live rent
  schedules re-created from the new date (`move_autopay` gains a `start: date` form); the deposit:
  when `move_deposit`, its due date or the plan's `first_due` shift by the same number of days
  (`installments.reschedule`); reminders and calendar follow the data (no stored items). Audited;
  the timeline shows "Start date changed <old> → <new> — <reason>".
- Route `ledger:leases:change_start` (`leases/<pk>/change-start/`, GET dialog + POST; **billing on a
  prospective lease, admin on an active lease** — the active case moves money, §9.1):
  the dialog shows the new date field, the reason, the effects list from `preview` (live on the
  date through a small JSON endpoint? no: a second GET with `?date=` re-renders the dialog — no
  script needed), the Keep-as-credit / Refund choice when paid, the "Move the deposit schedule too"
  tick, Cancel and "Change start date". The LEASE card offers it on a prospective lease and on an
  active one during its first month.

**(2) Free rent** (`services/free_rent.py`): `period(lease) -> (from, to) | None` from
`free_rent_days` and `free_rent_from` (`move_in`: from `started_on`; `next_month`: from the 1st
after the start month); `concession_for(lease, period_month, amount_due) -> (Decimal, text)`: the
days of the month inside the period over the month's days × the month's rent (the prorated first
month: the free days inside the lease's days), rounded to the cent, the last month taking the
remainder so the period's concessions sum exactly to `days/month-days` shares. `rent_run.create_month`
sets `concession` and `concession_text`; a change of the free period on Edit lease re-computes the
concession of unpaid future months only (billed months keep theirs; shown as a warning). The
invoice (builder and the PDF of PLAN-015c) shows the rent line at its amount and a DISCOUNT line
"Free rent per lease agreement (<from> – <to>)" of the concession; **to the processor** the rent
line carries Stax's per-line `discountRate` (the concession over the line's amount, to four decimals)
and the invoice `total` is the net — Stax's invoice API has a per-line discount rate and no discount
line (checked 2026-11-08; `lineItems[].discountRate` "Discount applied to this specific line item",
`total` required); if the sandbox rejects or mis-totals it (TODO-VERIFY), the fallback is the rent
line sent at the net amount with the two lines on our side only (§9.2); the LEASE card row "Free rent:
30 days, Dec 10 – Jan 8"; P&L: "Rent billed", "Concessions", "Net rent" (billed − concessions);
Revenue by month gains "Concessions" (CSV too). New lease: a "Free rent" row in the Terms card
(Days, Starts) and the Summary's first month shows the concession; Edit lease: a "Free rent" card
after Rent with the same two fields (Changes card lines "Free rent 0 → 30 days").

**(3) Deposit timing** (New lease, Move-in charges): the Deposit row gains "When": `with_invoice`
(default), `on_date` (a date field; the deposit charge gets that `due_date` and is not a move-in
line), `installments` (count 2–12, first date, cadence; a plan with `origin = deposit` created on
save; the deposit is not a move-in line). Reminders B–E apply to a deposit charge or occurrence by
its due date (the "deposits never" rule becomes "deposits with the move-in invoice never": they ride
the invoice). The LEASE card's deposit row: "$3,000.00 · due Dec 1" / "$3,000.00 · 1 of 3
installments paid" (`installments.Progress`). Lease-end and transfer keep `one_off_charges.settled`
(what was collected) — unchanged, pinned. **The portal** offers Pay for a deposit charge and for a
deposit installment like any other charge (today only an insurance charge is excluded), §9.4. Change start date moves the due date or plan only when
ticked.

## 4. Work breakdown (Opus; branch `design-v2/start-date-free-rent-deposit` from main; a commit
per task)

1. Migrations and model rules (void status, concession fields, free-rent fields, plan origin);
   P&L and Revenue treat void as waived; tests of the generated balance and the reports' totals.
2. Free rent: the service, `create_month`, the catch-up, the concession line in the builder and the
   invoice totals, the provider body (per §9 Q2); tests: from move-in, from the 1st, crossing a month
   boundary, with proration, the cent remainder, an invoice with the discount line.
3. Free rent on the pages: New lease row + Summary; Edit lease card + Changes card; the LEASE card
   row; P&L and Revenue columns; tests and the visual baselines of the touched pages.
4. Deposit timing: the New lease "When" control, the dated charge, the deposit plan (origin, the
   refusal lifted for it), reminders on deposit due dates, the LEASE card progress; tests incl.
   lease-end and transfer unchanged.
5. The move-in draft rebuild and the rent-alone void (service + provider job); tests with the
   FakeProvider (delete called once; VOID; `billed_by_invoice` kept as history; the charge voided).
6. Change start date: preview + apply, the dialog and route, the LEASE card action, autopay move
   from a date, the deposit tick, the credit and refund branches, the timeline words; tests for
   every branch of §3 (1) and the overlap refusal.
7. Self-review and report.

## 5. Test plan (must pass)

§4's, plus Fable's adversarial set: a start moved backwards across a month already paid (refused or
credited — §9 Q3); a free period longer than the lease's first two months; days = 366; a deposit
plan's first date before the start date (refused); Change start date with the deposit tick on a plan
that already collected one installment (only future occurrences move); the refund branch never
calls the provider from the view (job only); concessions never make a charge's balance negative;
P&L billed + concessions + net reconcile with the Revenue report for the same months; the invoice
PDF (015c) shows the discount line and the right balance; viewer/billing role rules on the new route.

## 6. Risk register

- **The processor's invoice body with a discount line** (§9 Q2): if the processor rejects a
  negative or discount line, the invoice's rent line carries the net amount and the PDF alone shows
  the two lines — a product difference the owner must accept.
- **Void of a rent-alone invoice at the processor**: `delete_invoice` on a sent invoice; the
  FakeProvider mirrors it; the live path is TODO-VERIFY.
- **Refund of first-month rent** is money leaving: the existing refund path's admin confirmation and
  job are reused unchanged; Change start date only queues it.
- **Reports**: two new columns; the CSVs' headers are pinned by tests that will be re-pinned.
- **Three migrations** on money tables; each reversible and tested.

## 7. Gate class and acceptance criteria

**Full gate.** Accepted when every §5 case exists and is green on both databases; the PR's CI green;
migrations apply and reverse; FROZEN_ROUTES moved by one; the QA-015 entry names the counts and the
verdict; the P&L and Revenue figures before and after are recorded in the entry.

## 8. Decisions taken in this plan

- A new `void` status for rent charges (not `waived`): void means never due; waived means forgiven.
- Concessions live on the rent charge (`concession`, `concession_text`), not in `ChargeAdjustment`
  (a store-to-store adjustment table, unused); the invoice shows them as a DISCOUNT line.
- The deposit plan is an installment plan with `origin = deposit`; the refusal lifts for it only.
- The Change start date dialog re-renders on a date change (no script); the effects come from one
  `preview` the POST re-runs before applying.

## 9. Owner's decisions (2026-11-08)

1. **Admin** for changing the start date of an active lease (it voids charges, creates credits, queues
   refunds); billing stays for a prospective lease.
2. **The concession to the processor:** Stax's invoice API supports a per-line `discountRate`, not a
   discount line (spec checked 2026-11-08). The rent line therefore carries the concession as its
   discount rate and the invoice total is the net, so the processor's invoice reads as ours; the
   fallback — the rent line sent net, the two lines on our side only — applies only if the sandbox
   rejects or mis-totals the rate (TODO-VERIFY at implementation; the owner is told which case holds).
   **Outcome (2026-11-09):** the sandbox keeps the rate and the net total but its hosted page shows the
   line gross with no discount (TODO-VERIFY-075), so the owner chose the fallback: the rent line net, the
   concession named in the line's details (PLAN-015g).
3. **An earlier start into a billed, paid month** is not refused: the extra days are billed as a
   prorated charge, with a confirmation stating the period and the amount before saving, admin only,
   recorded in the audit log.
4. **Deposits due on a date or in installments are payable through the portal** like any other charge.
5. The three draft decisions stand: the new `void` status, concessions stored on the rent charge and
   shown as a discount line, the deposit installment plan with its own origin.

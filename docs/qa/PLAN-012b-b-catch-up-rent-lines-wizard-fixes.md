# PLAN-012b-b — Rent run catch-up, rent billed by invoice, lease-end wizard fixes, P&L at lease end, splitting rent

**Date:** 2026-10-15 · **Origin:** the owner's Checkpoint 12b report of 2026-10-15 (items 3 to 9,
in writing; items 1 and 2 of that message were the clicks and the push). Finding **12b-1** (item
3) blocked Checkpoint 12b test 1. **Lead/QA:** Fable · **Implementer:** Opus · **Ships:** one
push after all batches are gated and the owner's checkpoint.

## 1. Restated objective

Seven corrections and additions found on staging. The rent run must **catch up** (every debit
date between a lease's start and today gets its charge, the first month prorated), and a lease
saved with a start date on or before today gets its first charge at once. The invoice builder
gets a **Rent** line (lease and month; the amount defaults to the rent, prorated for a partial
first month) that creates or attaches the month's rent charge so the run never bills it twice,
and the grid shows the month as billed by that invoice. A lease end that never reached the
statement step **leaves nothing behind**; one that did shows a banner with Continue and Cancel.
The lease-end and transfer wizards **create the final prorated rent charge themselves** on the
way to the statement instead of blocking on it. An **unpaid deposit** is a blocker, and the
statement says what was charged and collected. The **P&L at lease end** follows one stated rule
(deductions billed in the end month, the withheld deposit collected against them, the deposit
itself never revenue). **Rent charges** with a live Stax invoice or an autopay schedule are split
in one step through the same release path as one-off charges.

## 2. Affected packages

`backend/ledger` (`services/rent_run.py`, `leases.py`, `invoice_builder.py`, `grid.py`,
`lease_end.py`, `transfer.py`, `statements.py`, `pnl.py`, `installments.py`,
`one_off_charges.py`; views `run`, `lease_ends`, `transfers`, `stores`, `invoices`; templates;
models `LeaseEnd` (one timestamp), `InvoiceDraftLine` (rent fields), `RentCharge` (billing
invoice); migrations 0040, 0041), `backend/payments` (`one_off_invoices.py` release of a rent
invoice; `jobs/runners.py`; the daily tick calls the same `create_charges`), `docs/DECISIONS.md`
(ADR-0012 §7, the P&L rule), tests. Out of scope: Stax payment-method changes, the multi-line
invoice release, mail, Step 12d.

## 3. Decisions

### 3.1 Rent run catch-up (owner's item 3, finding 12b-1)

- **D1 — every missing debit, not only today's.** `rent_run.create_charges(day, actor)` keeps
  creating today's debits and then runs **catch-up**: for every lease that is active with a known
  channel and `started_on <= day`, every debit date from the start month to `day` (both debit
  days of a twice-monthly lease; a debit day that falls before `started_on` in the start month
  is due **on the start date**) whose `(period_month, seq)` has no rent charge yet gets one, with
  `due_on` that date, the amount from `first_month` (prorated when the start month and the
  switch is on; twice-monthly split as today), `proration_text`, status `due`, audited
  `rent_charge.created` with `"catch_up": true`. Months after `ended_on`, months with no rent in
  effect, and months already billed (item 4: a charge exists) are skipped. `ChargeRunReport`
  gains `caught_up: tuple[RentCharge, ...]`; the run page and the daily job's log say
  "Charges created: N. Created (catch-up): M. Already created: K." The catch-up looks back at
  most **24 months** from `day` (a guard against a stray start date), stated on the page.
- **D2 — the first charge on save.** `leases.create_lease` and `update_lease` (when `started_on`
  becomes a date on or before today, or the rent or debit days change for an unstarted month)
  call `rent_run.catch_up_lease(lease, today, actor)` inside their transaction, so the lease
  page shows the first charge at once. A lease starting in the future gets nothing until the
  start date; the daily run on or after the start creates it (D1). `rent_run.create_month(lease,
  period, actor, *, due_on=None)` is the shared single-month helper (used by D1, D2, D6 and D10).
- **D3 — the daily job.** The daily tick keeps calling `create_charges(today)`; nothing else.

### 3.2 Rent billed by invoice (owner's item 4)

- **D4 — the Rent line.** `InvoiceDraftLine` gains `rent_lease` (FK `Lease`, null) and
  `rent_month` (date, first of month, null); a line is a rent line when both are set (CHECK:
  both or neither; a rent line has no `fee_type`). `LineInput` gains `rent_lease: UUID | None`,
  `rent_month: date | None`. The builder form shows, beside the Item combo, **Rent** as a kind:
  pick the lease (the draft's lease preselected; a pending lease not yet started is allowed:
  leases of the operator in effect or starting later) and the month (a month input); the amount
  defaults to the lease rent for that month (`first_month`: prorated for the partial start
  month) and is editable; quantity fixed at 1. One rent line per lease and month in a draft
  (refusal `LINE_RENT_TWICE`), and refused when that month already has a rent charge that is
  paid, partly paid, or has a live Stax object (`LINE_RENT_BILLED`: "Rent <month> of store X is
  already billed/paid"). The Stax line item text is "Rent <Month YYYY>, store <label>" with the
  proration text as the detail.
- **D5 — the charge.** On send, a rent line creates the month's rent charge through
  `rent_run.create_month(lease, month, actor, due_on=the invoice's due date)` with
  `proration_text`, or **attaches** to the existing charge when the run already made it and it
  is unpaid with no live Stax object; the invoice is linked to the charge as `Invoice.rent_charge`
  for a single-rent-line invoice and through `InvoiceLineItem.rent_charge` (new FK, null) for
  every rent line, so `rent_run.charge_invoices` and `provider_invoice` find it, the lease-end
  blockers see a live invoice, the payment allocation pays the rent charge (sibling allocation
  as for one-off lines, PLAN-012c-a), `invoice_covered` marks it paid at Stax, and the catch-up
  and the run skip the month (the charge exists). `RentCharge.billed_by_invoice` (FK `Invoice`,
  null, migration 0041) names the invoice for the grid.
- **D6 — the grid.** A month billed by invoice shows "INV-2026-0012" in the cell's text (status
  from the charge as today); a rent paid before its month shows as paid in that month's cell
  (the charge's `amount_paid`), never as a credit: `last_month`/`_open_lines` already read the
  charge.
- **D7 — one invoice, several kinds.** Deposit and setup lines stay catalogue lines on the same
  draft (the move-in invoice: deposit + setup + first rent); the P&L counts the rent line's
  payment as rent collected by `settled_on` month and the deposit nowhere (`_add_invoice_lines`
  learns the rent kind: `part.category == "rent"`).
- **D8 — tests** (§6): rent invoiced before the lease starts, then the run on the debit day
  creates nothing for that month; rent invoiced after the run attaches to the existing charge;
  a second rent line for the same month refused; a rent line for a paid month refused.

### 3.3 A lease end left at the blockers step (owner's item 5)

- **D9.** `LeaseEnd` gains `statement_opened_at` (datetime, null; migration 0040), set the first
  time the statement step renders with no blockers (`lease_end.open_statement(end, actor)`,
  audited). A draft without it is **not in progress**: the store page shows no banner;
  `transfer.start` **adopts** such a draft (sets reason `transfer`, keeps the terms, audited
  `lease_end.adopted_by_transfer`) instead of refusing; the End-lease start page reuses it as
  today. A draft with it **is** in progress: the store banner reads "A lease end is in progress
  (statement step)" with two links, **Continue** (the statement step) and **Cancel** (the cancel
  confirmation, one POST), and the transfer start page shows the same two links in place of the
  bare sentence. A draft whose terms were never set is deleted by the daily job after 7 days
  (audited); not needed for the rule, but it keeps the table clean.
  Changed by PLAN-012f-a D: created and set when the statement step is saved, not when it opens.

### 3.4 The final rent charge in the wizards (owner's item 6)

- **D10.** `balance_blockers` no longer lists "would be owed at the end and is not charged yet":
  when the statement step opens (`open_statement`), the wizard calls `rent_run.catch_up_lease`
  for the lease and `rent_run.create_month` for the end month (prorated by `last_month`'s rule,
  `due_on` the end date when the debit day has passed), so the statement shows a real charge
  ("Rent 14 Oct, 1 of 31 days, $32.26") with its link, offset against the deposit like any other
  balance; `confirm` keeps its fallback. Only live Stax objects, active or paused schedules,
  pending payments, live plans and the unpaid deposit (D11) remain blockers; an open balance
  blocks a **transfer** only as the statement leaves it (after the offset), as today.
  Changed by PLAN-012f-a D: created and set when the statement step is saved, not when it opens.

### 3.5 An unpaid deposit at lease end (owner's item 7)

- **D11.** `lease_end.blockers` adds kind `DEPOSIT_UNPAID`: every deposit charge (refundable
  fee type) of the lease with a balance above zero and not waived or voided: "Security deposit
  $500.00 not collected: collect it or cancel the charge", linked to the charge page (its Cancel
  voids a hand-collected charge at once, audited as today; a live-invoiced one goes through the
  worker). `DepositLine` gains `charged`; the statement, the page and the PDF read "Deposit
  charged $500.00, collected $0.00" when they differ (and "Deposit held $500.00" as today when
  equal); `deposit_held` stays the collected amount.

### 3.6 The P&L at lease end (owner's item 8)

- **D12 — the rule (ADR-0012 §7).** A deposit charge is never a fee billed and a deposit payment
  never a fee collected (as today). **At lease end, the deductions other than the unpaid-balance
  offset are the fees billed of the end month**: the part covered by the deposit (the withheld
  amount less the offset) is billed and collected in that month (`withheld_deposits`, shown
  apart), and the rest is the "Lease-end balance" charge, billed in that month (it is a one-off
  charge due on the end date) and collected when paid. So billed ≥ collected always holds for
  the month, and the deposits-held line falls by the withheld and refunded parts. The offset part
  is neither billed nor collected here (it paid existing charges, counted where they were
  billed). Implementation: `_add_billed_and_collected` adds the withheld-less-offset amount to
  `fees_billed` as well as to `fees_collected` (one line); the lease-end balance charge is
  already billed by its due date. **First reproduce the owner's rows** in a test (Test Store
  Three: lease started and ended 2026-10-14, rent $1,000, deposit $500 paid by wire, damage
  deduction $800, transfer with the balance allowed): report the rows before the change, find
  why "Fees billed 1300.00 / Fees collected 1500.00" (the deposit or the setup line counted,
  the deposit payment counted as a fee, or the rent), fix what the rule says, and assert the
  rows after: October: rent billed $32.26, rent collected $0.00 (or the offset's part), fees
  billed $800.00, fees collected $500.00 "incl. $500.00 withheld deposit", deposits held $0.00,
  plus any setup line as it is. The "Show the rows" detail of that store goes into the QA
  report, and the staging rows after the deploy.

### 3.7 Splitting rent (owner's item 9)

- **D13.** The release of PLAN-012b-a extends to rent charges. A rent charge's own live
  provider invoice (`rent_run.provider_invoice`, single-charge) is released by the same
  `invoice_delete` job with the release parameter, the job's entity being the rent charge
  (`release_invoice` resolves the entity by `job.entity_type`; the local invoice void/DELETED,
  the rent charge untouched; `Invoice.rent_charge` and `billed_by_invoice` stay as history).
  **Rent autopay** (the lease's recurring schedule that would debit this month): the split
  **moves the schedule past the month**: the release job deletes the lease's rent schedule(s)
  at Stax as `stop_autopay` does and creates them again with `DTSTART` at the next month's
  debit (`leases._create_one` with `now` set to this month's debit instant), audited on the
  lease as `collection_schedule.moved_for_installments` with the plan id; the lease page says
  "Autopay resumes <date> (this month is collected in installments)". The refusal
  `AUTOPAY_WILL_DEBIT` goes; `LIVE_INVOICE` for rent goes; the rent charge whose month debit is
  `pending` stays refused. The installments collect as the lease does (autopay → one schedule
  each). Tests: a rent charge due later this month on an ACH lease split in three: the rent
  schedule is deleted and recreated with next month's DTSTART (fake), three one-occurrence
  schedules follow the release; an e-mailed rent invoice released; the lease page text; the
  lease end's blockers unaffected; a split refused while the month's debit is pending.
  TODO-VERIFY: Stax honours a new DTSTART next month without catching up (recorded).

## 4. Interface contract

`rent_run.create_charges(day, actor)` with `caught_up`; `rent_run.create_month(lease, period,
actor, *, due_on=None) -> tuple[RentCharge, bool]`; `rent_run.catch_up_lease(lease, day, actor)
-> list[RentCharge]`; `LineInput.rent_lease/rent_month`; `InvoiceDraftLine.rent_lease/rent_month`;
`InvoiceLineItem.rent_charge`; `RentCharge.billed_by_invoice`; `LeaseEnd.statement_opened_at`;
`lease_end.open_statement(end, actor)`; blocker kind `DEPOSIT_UNPAID`; `DepositLine.charged`;
`transfer.start` adopts a draft; `installments.release_kind` for rent; audit actions
`rent_charge.created` (`catch_up`), `lease_end.statement_opened`, `lease_end.adopted_by_transfer`,
`collection_schedule.moved_for_installments`; migrations `0040_lease_end_statement_opened`,
`0041_rent_lines` (draft line fields, `InvoiceLineItem.rent_charge`, `RentCharge.billed_by_invoice`).
No new dependency or environment variable; provider calls are the existing create, delete and
list.

## 5. Work breakdown (batches, each gated separately)

| Batch | Items | Owner | Done when |
|---|---|---|---|
| A | 3 (D1–D3), 5 (D9), 6 (D10), 7 (D11); migration 0040 | Opus | a lease created three days late shows its prorated first charge at once; the run catches up a month-late lease with two charges; a future lease gets nothing; a draft left at blockers blocks no transfer; the statement step creates the final rent charge; an unpaid deposit blocks and the statement says charged/collected |
| B | 4 (D4–D8), 8 (D12); migration 0041; ADR-0012 §7 (Fable) | Opus | the move-in invoice with a rent line bills the month once; the grid shows the invoice; the owner's P&L scenario reconciles |
| C | 9 (D13) | Opus | a rent charge on an ACH lease or with a live invoice splits in one step |

Fable: adversarial tests per batch, the gate per batch, ADR-0012 §7, RUNBOOK §17 updates,
DATA_OWNERSHIP rows, README, QA-012b-b, the checkpoint report.

## 6. Test plan (before implementation)

**A.** Catch-up: lease created three days after its start (one prorated charge at once, the run
creates nothing more); created a month late (two charges, the first prorated, each due on its
debit date); starting in the future (nothing until the start; the run on the start date creates
the prorated month); twice-monthly lease late (both seqs); a lease ended last month (nothing
after the end); the 24-month guard; the report's "Created (catch-up)"; idempotent across runs;
the daily job's call. Draft lifecycle: a draft left at blockers → no banner, transfer start adopts
it (reason transfer, terms kept, audited); a draft at the statement → banner with Continue and
Cancel, transfer start shows both links, Cancel from the banner deletes it; the 7-day sweep. Final
rent charge: statement step opens → the end month's prorated charge exists with the text and
link, listed as open, offset by the deposit; the transfer's balance blockers no longer say "not
charged yet"; confirm's fallback still works. Unpaid deposit: blocker text and link; partly paid
deposit; cancelling the charge clears it with the audit row; the statement's "charged/collected"
line on the page and in the PDF. **Adversarial (Fable):** the catch-up never creates a charge
for another lease's month or past `ended_on`; two concurrent runs create each charge once
(PostgreSQL); the adopted draft cannot be adopted by a second store's transfer; a viewer sees the
banner without the Cancel link; the created final charge never exceeds the prorated amount
whatever the deductions post.
**B.** Rent line: the amount defaults to the prorated first month and is editable; one per lease
and month; refused for a paid or live month; send creates the charge with the proration text and
links the invoice; the run and the catch-up skip the month; rent invoiced after the run attaches
to the existing unpaid charge; payment on the invoice pays the rent charge (and the other lines
by sibling allocation); the grid cell shows the invoice number and "paid" in that month when paid
in advance; the P&L counts the rent line as rent collected by settled month, the deposit line
nowhere; the owner's Test Store Three rows before and after D12; billed ≥ collected under the
property test with random lease ends. **Adversarial (Fable):** a rent line cannot bill another
operator's lease; the rent line's amount cannot exceed `AMOUNT_MAX` or go below a cent; a forged
`rent_month` not on the first of a month is refused; a draft with two rent lines through forged
positions is refused; the invoice's charge cannot be double-paid by the run's charge; totals once.
**C.** As D13 lists. **Adversarial (Fable):** the schedule move cannot skip two months; a
release on a rent charge of another lease by a forged job entity does nothing; a late debit of
the old schedule (webhook) lands on the rent charge and puts the plan in attention; a lease end
during the moved schedule's gap sees no live schedule for the month.

## 7. Risk register

- Money: the catch-up creates charges retroactively (audited, idempotent, bounded to 24
  months); the rent line's attach rule must never attach to a paid or live charge.
- Provider: the rent schedule move is a delete and a create at Stax; a failure between them
  leaves autopay off with a visible failure and Retry (as a failed release today).
- Reporting: D12 changes historical P&L rows for past lease ends (they move toward the rule); the
  owner sees the before/after rows in the report.
- Schema: two migrations; 0041 touches `rent_charges` (nullable FK) and `invoice_line_items`.

## 8. Acceptance criteria

Each batch's gate green on both engines; the §6 cases present; on staging after the push:
Checkpoint 12b test 1 (store Istanbul shows "13–31 Oct, 19 of 31 days of $2,000.00 =
$1,225.81"); Test store 2 transfers after a lease end left at blockers; Test Store Three's P&L
rows read as D12; a move-in invoice with deposit + setup + first rent bills the month once; a
rent charge on an ACH lease splits in one step.

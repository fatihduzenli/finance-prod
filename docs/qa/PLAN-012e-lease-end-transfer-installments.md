# PLAN-012e — Step 12b: lease end, operator transfer, installment plans, proration

**Date:** 2026-10-13 · **Origin:** owner's Step 12b text of 2026-10-13 (rules confirmed in writing:
deposits refundable at lease end by Zelle, wire or check, never through Stax, liabilities excluded
from revenue and P&L; the outgoing operator settles every balance before a transfer completes and
the incoming operator pays their own deposit and setup charges; installments carry no interest and
a returned installment follows the NSF rule). **Lead/QA:** Fable · **Implementer:** Opus ·
**Checkpoint:** one report before the push (owner's "Report with a checkpoint before I push").

## 1. Restated objective

Four pieces of lease-lifecycle work on top of Checkpoint 12. **Proration (4a):** a lease that
starts or ends mid-month is charged for the days used; the lease form gets "Prorate first
month" (default on) and the end-of-lease wizard "Prorate last month" (default on); the first
rent charge and the final statement show the arithmetic; the existing lease-start flow, which
today bills a full first month, is fixed with a test for a lease starting on the 13th. **End of
lease:** a wizard from the store page that takes the end date and reason, blocks while any Stax
invoice is live or any schedule is active on the lease and links to each, builds the final
statement (prorated last rent, open balance, deposit held, typed deductions, refund due or
amount owed), records the deposit refund by Zelle, wire or check or the remaining debt as a
charge, releases the deposit liability, and on an explicit "are you sure" ends the lease, leaves
the store vacant, stores a PDF statement under the store's documents, e-mails it to the operator
when mail is on, and audits every step. **Transfer:** a wizard from the store page that picks or
creates the incoming operator, blocks until the outgoing lease's balance is zero and nothing is
live at Stax, runs the end-of-lease steps for the outgoing lease, then creates the incoming lease
(start, rent, deposit, setup charges from the catalogue) with the deposit and setup invoice as a
builder draft for review, never leaving two current leases on the store. **Installments:** a
plan from any open charge (2–12 installments, first date, cadence, equal amounts with the
rounding cent on the last, editable before confirm), each installment its own charge collected
as the parent is, the parent showing the plan's progress; a returned installment raises the NSF
fee by the existing rule and puts the plan on the attention list; plans can be rescheduled or
cancelled; the grid and reports count the money once, under the parent. Deposits stay out of
revenue everywhere and the refund is a liability release.

## 2. Affected packages

`backend/ledger` (models and migrations, `services/{leases,rent_run,deposits,one_off_charges,
receivables,pnl,reports,attention,documents,collections}.py`, new `services/{lease_end,
transfer,installments,proration,statements}.py`, views, templates, forms, urls, registry),
`backend/mail` (attachments), `backend/payments/jobs` (two job kinds), `backend/tests`, `docs/`.
**Out of scope:** Step 12d (invoice-level payment entry), the Step 13 import, any Stax call that
does not exist today (every provider call in this plan is one the adapter already makes:
schedule create/update/delete, invoice create/send/delete, mark paid), cards, surcharges.

## 3. Decisions

### 3.1 Proration (owner's 4a)

- **D1 — arithmetic.** `proration.prorated(rent: Decimal, month: date, first_day: int, last_day:
  int) -> Prorated(days_used, days_in_month, amount, text)`. **The amount is `rent × days_used ÷
  days_in_month`, rounded to the cent once (half up).** The owner's rule text says "daily rate
  rounded to the cent, × days", which for the owner's own example gives 64.52 × 19 = **$1,225.88**;
  the owner's example answer is **$1,225.81**, which is the single rounding. The plan follows the
  example (fairer; the sheet's practice as the example shows) and **asks the owner to confirm at
  the checkpoint**; switching to the per-day rounding is one line and one test. The text is
  "13–31 Oct, 19 of 31 days of $2,000.00 = $1,225.81" (en dash, the month's short name, thousands
  separators as the owner wrote). Start and end days both count. A lease starting on the 1st or
  ending on the last day is a full month (no text).
- **D2 — first month.** `Lease.prorate_first_month` (boolean, default true; on the lease form as
  "Prorate first month"). `rent_run.leases_due` / `create_charges`: for the charge whose
  `period_month` is the lease's start month, when `started_on.day > 1` and the switch is on, the
  month's amount is `prorated(rent, month, started_on.day, last day)`; the rent charge gets
  `proration_text` (new nullable text column on `rent_charges`, shown on the charge page, the
  grid cell title and the statement). With two debits a month, `debit_amounts` splits the prorated
  amount as it splits a full one; a debit day before the start date in the start month is
  skipped as today. **Existing behaviour fixed:** a lease starting on the 13th today bills a full
  month; after D2 it bills 19/31. Leases already billed are not rewritten (no backfill; the
  Step 13 import maps the sheet's amounts).
- **D3 — last month.** In the wizard (3.2), "Prorate last month" (default on): the end month's
  rent is `prorated(rent, month, 1, ended_on.day)`. If a rent charge for that month exists and is
  unpaid or partly paid, its `amount_due` is reduced to the prorated amount (audited
  `rent_charge.prorated`, never below `amount_paid`; if paid beyond it, the excess is a statement
  credit); if it is fully paid, the excess is a **credit** line on the statement; if no charge
  exists yet, a final rent charge for the prorated amount is created (`collection` manual, no
  Stax) and listed as open balance. The switch off means: the month's charge stands as generated
  (or a full month is created if the debit day lies on or before the end date and no charge
  exists). Months after the end month are never charged (`_skip_reason` learns `ended_on`).

### 3.2 End of lease (owner's 1)

- **D4 — entry point and model.** Store page button **End lease** (admin, like `leases:end`
  today) for the store's current lease; the old `leases:end` route redirects to the wizard. A
  `LeaseEnd` row (`lease_ends`: `lease` one-to-one, `ended_on`, `reason` choice `moved_out |
  terminated | transfer | other`, `prorate_last_month`, `status` `draft | confirmed`,
  `statement_json` (the figures as computed at confirm, guarded JSON), `document` FK nullable
  (the PDF), `refund_payment` FK nullable, `debt_charge` FK nullable, `created_by`, `confirmed_at`,
  timestamps) holds the wizard's state so the four steps can be left and resumed; one draft per
  lease, deleted on Cancel.
- **D5 — steps.** (a) date (default today, New York; not before `started_on`; not in the future
  by more than 0 days, as `end_lease` refuses today) and reason. (b) **Blockers**: every one-off
  charge on the lease with a live provider invoice (`live_provider_invoice`) or a provider
  schedule (`provider_schedule`), every rent or insurance schedule in state active or paused with
  a schedule ref (`leases.live_schedules`), every rent charge or one-off charge with a pending
  (unsettled) provider payment; **and (amended 2026-10-14, QA finding) every installment plan
  that is live (`active` or `attention`) on a rent or one-off charge of the lease**, linked to
  the plan page ("let it complete or cancel it first": the last-month proration would otherwise
  cut a parent whose installments still bill the original split); each listed with a link
  (charge page, lease page's autopay controls, plan page). The wizard cannot pass (b) while the
  list is non-empty; it re-checks at confirm.
  "Collect or cancel first": the existing cancel / pause actions are the way; the wizard makes
  no provider call itself. (c) **Final statement** (`statements.final(lease_end) -> Final`):
  prorated last rent (D3), every open balance on the lease (rent, insurance, one-off, by
  `receivables.open_balances` filtered to the lease, deposits excluded as today), deposit held
  (the lease's refundable setup charges' settled amount less any resolution), **deductions**:
  typed lines (`LeaseEndDeduction` rows: `kind` `damage | cleaning | unpaid_balance | other`,
  description, amount > 0; "unpaid balance offset" pre-filled with the open balance and editable
  down, never above it), credits (D3 overpayment), **refund due** = deposit held − deductions
  (when ≥ 0) or **amount owed** = deductions − deposit held + open balance not offset. The
  arithmetic is shown line by line with the proration text. (d) **Settlement**: refund due > 0 →
  method (Zelle, wire, check), date, reference, note → on confirm `deposits.resolve_deposit(kind
  refunded, amount, method, …)` for the refunded part and `kind withheld` for the deductions
  part (two resolutions are not allowed by the one-to-one `deposit_resolution`; so the service
  gains `resolve_deposit_split(charge, refunded, withheld, …)` writing one resolution row with
  `amount` refunded and `withheld_amount` withheld, the existing columns); amount owed > 0 → a
  one-off charge `category other`, description "Lease-end balance", collection manual, due the
  end date + 15, on the lease, linked from the statement; the unpaid-balance offset reduces the
  open charges it offsets by recording a manual payment? **No**: an offset is a deposit
  resolution `withheld` whose amount is applied to the open charges oldest first as settled
  manual payments of method `other`, reference "deposit offset", so the charges read paid and the
  deposit reads withheld, both audited (the money moved from the liability to the receivable).
  (e) **Confirm** ("are you sure" page listing every effect): in one transaction, re-check (b),
  apply D3, write the deductions and the settlement, call `leases.end_lease` (which gains
  `reason` and loses nothing), set `Store.status` unchanged (vacancy stays derived, as today's
  attention item; the owner's "store becomes vacant" is satisfied by the store having no lease in
  effect; the store page shows "Vacant since <date>"), render the PDF (3.4), store it under the
  store's documents with kind `final_statement` (new `DocumentKind`, store only) through
  `documents.upload` (the 10c checks run; the statement holds no account numbers), queue the
  e-mail (3.5), and audit `lease_end.confirmed` with the figures. The `LeaseEnd` row keeps the
  figures as confirmed.

### 3.3 Transfer (owner's 2)

- **D6 — wizard.** Store page button **Transfer to another operator** (admin). Steps: (a) pick
  an existing operator (search by name, not archived) or create one inline with the existing
  `OperatorCreateForm` (SMS fields included; the sync job queues as today); (b) blockers = D5(b)
  **plus** a non-zero open balance of the outgoing operator on this lease (listed per charge) and
  a deposit not yet held or already resolved is not a blocker; (c) the end-of-lease steps for the
  outgoing lease (D5 c–e, reason fixed to `transfer`, the statement and refund as there), then
  the incoming lease: `started_on` (default the day after the end date; **must be ≥ end date + 1**),
  rent, debit day(s), channel (as the lease form), deposit amount (default the catalogue's
  `security_deposit.default_price` or the outgoing deposit's amount), setup charges ticked from
  the catalogue (as the 10b setup checklist); `leases.create_lease` for the incoming lease
  (`prorate_first_month` on by default); the deposit and the ticked setup charges become **one
  builder draft** (`invoice_builder.create_draft` on the incoming operator and lease, lines from
  the catalogue, memo "Move-in charges for store <label>") that staff review and send; no charge
  is created until the draft is sent (the builder makes them). (d) one `Transfer` row
  (`lease_transfers`: `store`, `outgoing_lease`, `incoming_lease`, `lease_end`, `draft`,
  `created_by`, `confirmed_at`) and audit `lease_transfer.confirmed`; the PDF and e-mail of the
  outgoing statement as in D5. **Invariant:** after confirm, no day has two leases of the store
  in effect (`_in_effect`); a DB CHECK cannot express it, so the service refuses an incoming start
  ≤ the end date and a test proves the invariant over the hand-over.

### 3.4 PDF statement

- **D7 — a new dependency: `reportlab`** (BSD, pure Python, pinned in `pyproject.toml`; flagged
  for the owner's review at the checkpoint). `statements.render_pdf(final) -> bytes`: the company
  name, the operator, the store, the lease dates, the lines of (c), the settlement of (d), the
  proration text, "Prepared on <date>"; no account or card numbers (the statement names methods
  and references only; references are guarded text). The bytes go through `documents.upload` as a
  PDF (`application/pdf`), so the 10c text check runs on our own output (a test proves it passes
  and that a reference holding a routing-and-account pair is refused before the PDF exists). The
  PDF is also offered for download from the lease-end page.

### 3.5 Statement e-mail

- **D8 — attachments.** `mail.Message` gains `attachments: tuple[Attachment, ...] = ()`
  (`Attachment(filename, content_type, data)`); `mime_bytes` builds `multipart/mixed` when
  there is one; both senders unchanged otherwise; the catch-all keeps the attachment. A seeded,
  admin-editable `MessageTemplate` with code `final_statement` (subject and body with the merge
  fields `{operator}`, `{store}`, `{company}`, `{refund_due}`, `{amount_owed}`, `{end_date}`;
  `ledger/merge.py` whitelist extended). `OutboundMessage.step` becomes nullable and the row
  gains `kind` (`ladder | final_statement`, CHECK: `step` required for `ladder` only) and
  `document` FK nullable (the attachment); `collections._queue` generalised to `messages.queue`.
  The e-mail is queued only when `MAIL_ENABLED` would send (the `NullSender` records
  `suppressed` as today, so the log shows it was not sent); outside production the catch-all
  rule applies unchanged.

### 3.6 Installments (owner's 3)

- **D9 — model.** `InstallmentPlan` (`installment_plans`: `rent_charge` or `one_off_charge`
  (exactly one, CHECK), `operator`, `lease`, `count` (2–12), `first_due`, `cadence` `weekly |
  biweekly | monthly`, `collection` (the parent's: `autopay_due`, `email`, `manual`), `status`
  `active | attention | completed | cancelled`, `created_by`, `confirmed_at`, `cancelled_at`,
  `note`, timestamps). Each installment is a `OneOffCharge` with new fields `plan` (FK) and
  `installment_no` (1..count), category `installment` (new `OneOffCategory`), description
  "Installment n of N: <parent description>", price = equal share rounded down to the cent with
  the remainder on the last installment (`installments.split(total, count) -> list[Decimal]`,
  sums exactly), due dates from `first_due` and the cadence (monthly = same day of month, clamped
  to the month's last day), collection as the parent (autopay → one one-occurrence schedule per
  installment as 10d does; email → one invoice per installment; manual → nothing at Stax). The
  **parent** keeps its price; while the plan is active the parent is **excluded from open
  balances, aging, arrears and the attention list** (its balance is the plan's) and shows
  "Installment plan: n of N paid, $X of $Y" on its page, with the installments listed; a payment
  on an installment allocates to the installment only; the parent's `amount_paid` is not
  touched (the parent is "covered"); when every installment is paid the plan is `completed` and
  the parent is marked `paid` through a `recompute` that understands plans (its `amount_paid`
  set to its price with a `covered_by_plan` audit, no payment row). The parent's own live Stax
  invoice or schedule is a blocker for splitting (cancel or collect first, as in 3.2); a rent
  charge whose month schedule has already debited is not splittable (`pending`).
- **D10 — "Split into installments."** On a one-off charge page and a rent charge page (billing,
  balance > 0, no live invoice or schedule, no plan): count, first date (≥ today), cadence; the
  preview lists the installments (dates and amounts) and the staff may edit each date and amount
  before confirm as long as the amounts still sum to the parent's balance and dates stay
  ascending; confirm creates the plan and the charges and queues the provider jobs per
  installment. From an invoice page the button goes to the invoice's charge when the invoice has
  one charge; a multi-line builder invoice says "split each line from its charge page" (scope
  note for the owner).
- **D11 — attention, reschedule, cancel.** A returned installment debit: the existing
  `nsf.reschedule` applies to the returned payment (re-debit + fee by the existing rule, agreed
  by phone), and the plan becomes `attention` (new attention kind `INSTALLMENT_RETURNED`, item
  per plan, cleared when the plan has no returned, unresolved payment). **Reschedule** (billing):
  a new first date (and optionally cadence) for the **remaining** unpaid installments; their due
  dates shift (provider schedules updated, e-mailed invoices' due dates updated through the
  existing update call where one exists, else re-created), audited. **Cancel** (admin, note): the
  remaining unpaid installments are cancelled through the existing cancel path (their Stax
  invoices or schedules deleted by the worker), the parent returns to a single open charge for
  the unpaid remainder (its `amount_paid` set to the installments' paid sum, audited), the plan
  `cancelled`.
- **D12 — grid, reports, ledger.** Installment payments count under the parent: `pnl` and
  `reports` attribute an installment's payments to the parent's **category** (rent parent or
  `arrears` → rent collected; other one-off parent → fees collected) **in the month the payment
  settled, like every other payment** (amended 2026-10-14: the first wording said "the parent's
  month", which would let a later installment payment rewrite a closed month); installments
  are never billed lines themselves (`fees_billed` excludes category `installment`); the grid
  cell of a rent parent shows the plan's progress; the payments ledger lists installment
  payments with the parent named; `receivables.open_balances` lists the unpaid installments
  (due on their dates) and not the parent. The property test of open balances gains plans.

### 3.7 Deposits (owner's 4)

- **D13.** No change to the exclusion rules (ADR-0007 holds everywhere already); the wizard's
  settlement writes through `deposit_resolutions` so `deposits_held`, the P&L footer and the
  statement show the release; the refund is a `refund_deposit` payment, never revenue or expense;
  a withheld amount becomes revenue in its month as today (ADR-0007). ADR-0012 (Fable) records
  the lease-end handling.

## 4. Interface contract

- `proration.prorated(rent, month, first_day, last_day) -> Prorated`; `Lease.prorate_first_month`;
  `RentCharge.proration_text`.
- `lease_end.start(lease, actor) -> LeaseEnd`, `.set_terms(end, ended_on, reason, prorate)`,
  `.blockers(lease, today) -> list[Blocker(kind, text, url)]`, `statements.final(end) -> Final`,
  `lease_end.set_deductions(end, lines)`, `.set_settlement(end, method, on, reference, note)`,
  `.confirm(end, actor, *, confirmed: bool)`, `.cancel(end, actor)`; routes
  `ledger:stores:end_lease` (start), `ledger:lease_ends:{terms,blockers,statement,settlement,
  confirm,cancel,pdf}`; `DocumentKind.FINAL_STATEMENT`; `deposits.resolve_deposit_split`.
- `transfer.start(store, actor)`, `.set_incoming(...)`, `.blockers`, `.confirm`; routes
  `ledger:stores:transfer`, `ledger:transfers:{operator,blockers,end,incoming,confirm,cancel}`.
- `mail.Attachment`, `mail.Message.attachments`; `messages.queue(kind, operator, template,
  context, *, document=None, actor=None)`; `OutboundMessage.kind`, `.document`, `.step` nullable;
  seeded template `final_statement`; merge fields above.
- `installments.split(total, count)`, `.schedule_dates(first, cadence, count)`,
  `.create_plan(parent, count, first_due, cadence, lines, actor)`, `.reschedule(plan, first_due,
  cadence, actor)`, `.cancel(plan, note, actor)`, `.progress(plan)`; `OneOffCategory.INSTALLMENT`;
  `OneOffCharge.plan`, `.installment_no`; attention kind `INSTALLMENT_RETURNED`; routes
  `ledger:one_off_charges:split`, `ledger:charges:split`, `ledger:plans:{detail,reschedule,cancel}`.
- Job kinds: none new for the provider (the existing `schedule_create`, `schedule_update`,
  `schedule_delete`, `invoice_create`, `invoice_send`, `invoice_delete`, `message_send` are
  reused); one new job `lease_end_documents` (render the PDF, store it, queue the e-mail) run by
  the worker so the confirm request stays fast and the storage call is retried as documents do.
- Migrations: `0034_proration`, `0035_lease_end_transfer`, `0036_messages_kind`, `0037_installments`
  (schema; PostgreSQL guard triggers for every new text column; the seeded template as a data
  step). New dependency `reportlab` pinned. New environment variables: none.

## 5. Work breakdown (three batches, in order; Fable gates each)

| Batch | Scope | Done when |
|---|---|---|
| A | D1–D3 proration; D4–D5 lease-end wizard with the statement, deductions, settlement, confirm; D7 PDF; D8 mail attachments and the `final_statement` message; D13 | a lease starting on the 13th bills 19/31 with the text; the wizard on a store with a live invoice lists it and will not pass; a clean lease ends with a refund recorded, the deposit released, the PDF under the store's documents, the e-mail queued (suppressed with mail off), the store "Vacant since"; `leases:end` redirects |
| B | D6 transfer wizard | a transfer on a store with an open balance lists it and will not pass; a clean transfer ends the outgoing lease (statement, refund), creates the incoming lease the day after, and leaves one builder draft with the deposit and the ticked setup lines; no day has two leases in effect |
| C | D9–D12 installments | a $1,000 charge split in 3 monthly gives 333.33, 333.33, 333.34 on the right dates with the parent's collection; a returned installment raises the fee and the attention item; reschedule shifts the rest; cancel returns the remainder to the parent; grid, P&L and ledger count once |

Docs by Fable after C: ADR-0012, RUNBOOK §17, DATA_OWNERSHIP, README matrix, help text
review, QA-012e, the checkpoint report.

## 6. Test plan (before implementation)

**Proration:** 13–31 Oct of $2,000 = $1,225.81 (and $1,225.88 if the owner chooses per-day
rounding; one test names the rule); 1st and last day = full month, no text; February in a leap
year; a 31-day and a 30-day month; two debits a month; a lease starting on the 13th generates
19/31 with the text on the charge, the grid title and the statement; switch off → full month;
the end month with a charge unpaid → reduced; fully paid → credit; no charge → created; months
after the end never charged.
**Lease end:** blockers each alone (live invoice, active schedule, paused schedule with a ref,
pending provider payment) and together, with links; the statement arithmetic for: no deposit;
deposit = deductions; deposit > deductions (refund); deposit < deductions (owed); the
unpaid-balance offset paying the oldest charge first and never above the balance; credit from
an overpaid last month; a refund by each method with the reference guarded; confirm re-checks
the blockers (a live invoice created between steps refuses); the PDF stored under the store
with kind `final_statement`, downloadable, its text passing the guard; the e-mail queued with
the attachment, `suppressed` with mail off, catch-all outside production; audit rows for every
step; a viewer and billing get 403 on the wizard; Cancel deletes the draft and writes nothing.
**Transfer:** blocked by a non-zero balance (listed per charge) and by each D5 blocker; the
incoming start ≤ end refused; the hand-over day; the incoming lease, the draft with the deposit
and the setup lines (and nothing created at Stax until the draft is sent); the operator created
inline queues its sync job; the invariant over 400 random days; audit.
**Installments:** split rounding (1000/3, 100/7, 0.05/2 → 0.02 and 0.03 by D9 (rounded down,
the remainder on the last; refused only when a share would be below one cent, 0.01/2; amended
2026-10-14), 999.99/12); dates for each cadence incl. the 31st monthly (clamped) and leap February; count
1 and 13 refused; a first date in the past refused; edits that break the sum or the order
refused; the parent excluded from open balances, aging, arrears and attention while active;
each collection kind creates the right provider objects (fake), none for manual; a payment on
installment 2 only; all paid → plan completed, parent paid without a payment row; a returned
installment → NSF reschedule works on it, fee by the rule, plan `attention`, the item shown,
cleared on resolution; reschedule shifts only unpaid ones and updates Stax; cancel voids the
rest, deletes at Stax through the worker, returns the remainder to the parent; the P&L and
ledger totals equal those of the unsplit charge paid the same amounts; the grid shows progress.
**Adversarial (Fable):** the wizard cannot end a lease of another store by a forged id; the
settlement cannot refund more than the deposit held; a deduction with a card number is refused;
the PDF never contains an account number (the guard runs on our own bytes); the e-mail never
goes to a real address outside production; a transfer cannot create the incoming lease without
ending the outgoing one (atomicity under a forced failure); an installment plan cannot be built
on a charge of another operator's lease; two concurrent confirms leave one plan; totals never
double count under random plans (property test); a viewer cannot start any wizard.

## 7. Risk register

- Money state: the statement moves a liability (deposit) to a receivable (offset) or to a refund
  payment; every line is a row with an audit entry; nothing is sent to Stax by the wizard.
- New dependency (`reportlab`) for the PDF; alternative considered: the browser's print (no
  stored document). The owner reviews it at the checkpoint.
- The mail package changes shape (attachments); the catch-all and the key fence are retested.
- Proration rule: one line, flagged for the owner's confirmation.
- Installments touch the allocation and reporting paths of Checkpoint 12; the property test of
  open balances and the P&L equality test are the guard.
- Irreversible: a confirmed lease end (the lease is ended; a mistaken one is reopened by an admin
  through the lease form as today, the resolution and refund rows stay as history).

## 8. Acceptance criteria

Gate green on both engines after each batch and after C; the §6 cases present; `reportlab`
pinned and reviewed; the checkpoint report lists, for the owner's staging clicks: a lease
starting on the 13th; End lease on a store with a live invoice (blocked) and on a clean one
(refund, PDF, suppressed e-mail, "Vacant since"); Transfer on store X; a 3-installment split
with one returned installment; the P&L unchanged by a split.

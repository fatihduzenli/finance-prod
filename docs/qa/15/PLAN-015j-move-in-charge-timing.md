# PLAN-015j — New lease, Move-in charges: an editable deposit and a "When" per charge

**Owner's request (2026-11-13), two items from the review of step 5 on staging. Gate class: full
(billing).** Plan by Fable; implementation by Opus; gate and sign-off by Fable. Record:
`docs/qa/15/QA-015.md`. Branch `015j/move-in-when` from main 463dc66. One PR.
**Status: accepted. The owner answered §9 on 2026-11-13 and added requirement 7 (§10).**

## 1. Objective

On the New lease page's step 5, the Security deposit row becomes an editable price like every other
row, and the single "Deposit: when" block under the list is replaced by a "When" select on every
row. Each priced row chooses "With the first invoice" (the default, today's behaviour), "Due on a
date" (its own charge with that due date, not a line of the move-in invoice) or "In installments"
(2 to 12 payments from a first date, through the installment-plan engine). The deposit keeps the
PLAN-015d behaviour; the same code path serves the other one-time charges. The Summary card and the
lease page state the choice per charge. No stored value changes for existing leases; no migration.

## 2. What the code does today (read 2026-11-13, main 463dc66)

- **Why the deposit row is not editable (item 1).** It is not a disabled field. The deposit is one
  form field (the price of the first refundable fee type, `LeaseCreateForm.deposit_type`), rendered
  once, in the Terms card of step 2 under the label "Deposit" (`_new_lease.html:23`). Step 5 shows
  an `<output id="deposit-mirror">` that copies what is typed in Terms (`_new_lease.html:55-56`,
  `new_lease.js:147,210-212`); with nothing typed it shows a dash. This is PLAN-014b item 1,
  decision 4, taken from the owner's reference `docs/design/new-lease-dark-laptop.png`. **No
  billing rule depends on it**: the service receives one price either way.
- **Deposit timing (PLAN-015d §3 (3)).** `deposits.DepositTiming` (when, due_on, count, first_due,
  cadence) and `deposits.create_lease_with_deposit`: "on a date" saves the deposit charge with that
  `due_date` and `dated=True` and leaves it off the move-in draft; "in installments" also creates an
  `InstallmentPlan` of origin `deposit`, collected as the lease's channel
  (`installments.deposit_collection`: the bank debit when the lease's account can take it, else
  recorded by staff). Nothing else is stored: the timing of a saved lease is its charge's `dated`
  flag and its plan.
- **The other one-time charges.** Each is a setup charge (`category=setup`, `collection=none`),
  always a line of the move-in draft (`views/leases.py:519-526`). Each row's "More" already holds a
  "Due date" field ("Empty is the start date"): it is stored on the charge, but the charge still
  rides the first invoice, whose own due date is what the operator sees.
- **Reminders.** A non-refundable setup charge that is owed and not on a plan is already a
  receivable by its own due date (`receivables._one_off_balances`), so the reminder ladder follows
  it. A deposit is held money and enters the ladder only when dated (`receivables.dated_deposits`).
- **Installments of a non-deposit charge** exist as the charge page's "Split into installments"
  (origin `split`); the plan is collected as its parent is, which for a setup charge is "manual".
- **The model already allows it:** `one_off_charges_dated_only_setup` permits `dated` on any setup
  charge. The page has no review step; its Summary card is the review.

## 3. Scope

**In (backend/ledger):** `forms.py` (`LeaseCreateForm`), `templates/ledger/leases/_new_lease.html`,
`static/ledger/new_lease.js`, `finance` app.css (the row and its phone layout),
`services/deposits.py` (the timing type and the save, generalised), `services/installments.py`
(the lease-channel collection generalised; the preview sentence), `views/leases.py` (the save and
the draft's charge list), the lease page's deposit text and charge rows (`services/workspace.py`,
`services/deposits.card_text`, the charge row template), tests, the four New lease baselines and
any lease-page baseline that moves.

**Out:** Edit lease; the transfer wizard's setup rows; Change start date (its "move the deposit
schedule" tick keeps acting on the deposit only, §9 Q6); the portal; reminders' rules; the
processor adapter; models and migrations (none); the charge page's own Split.

## 4. Interface contract

- **Timing type.** `deposits.DepositTiming` becomes the timing of any setup charge: renamed
  `ChargeTiming` in a new small module `services/charge_timing.py` with `DepositWhen` → `When`
  (values unchanged: `with_invoice`, `on_date`, `installments`); `deposits` re-exports nothing (its
  callers are updated). Labels: "With the first invoice", "Due on a date", "In installments".
- **Form fields, per fee type `<name>`:** `<name>_when` (optional, default `with_invoice`),
  `<name>_on` (date), `<name>_count` (2–12), `<name>_first_due` (date), `<name>_cadence` (monthly
  default; §9 Q3). The five `deposit_*` fields and the row's "More → Due date" field are removed
  (§9 Q2). `LeaseCreateForm.timings() -> dict[fee type pk, ChargeTiming]` replaces
  `deposit_timing()`.
- **Validation (per row, the deposit's rules of 015d for every row):** a row without a price
  ignores its When fields and saves nothing; "Due on a date" needs a date not before today; "In
  installments" needs a count 2–12 and a first date not before today nor before the start date. No
  new required field: an untouched row is `with_invoice`.
- **Save.** `create_lease_with_deposit` becomes `charge_timing.create_lease(fields, …, timings=…)`,
  one transaction: the lease and its charges (`create_lease_with_checklist`, unchanged), each
  `on_date` charge `dated=True` with that due date, each `installments` charge a plan through
  `installments.create_plan` — origin `deposit` for the deposit (unchanged), origin `split` for
  the others, both collected by the lease's channel (`deposit_collection` renamed
  `lease_channel_collection`, §9 Q4). A refused plan raises `SetupRefused` naming the row; nothing
  is saved.
- **Move-in draft.** Its lines are the rent line and every charge whose timing is `with_invoice`.
- **Preview sentence.** One pure function beside `settlement_preview` sharing `settlement_lines`:
  "3 payments of $500.00: Dec 1, 2026 … Feb 1, 2027", with ", the last $333.34" when the last
  differs. The page shows it under the row from the server on a re-render and live from the page
  script computing the same split (cents, last takes the remainder), pinned equal by a test over a
  table of cases.
- **Page.** Row grid: name, price, When select (pill, `disabled` until the price holds a value;
  a disabled select is not submitted and reads as `with_invoice`), usual price, More. Under the
  row, on their own line: the date for "Due on a date"; count, first date (and cadence) and the
  preview sentence for "In installments". Phone (the one phone block of app.css): the select drops
  under the price on its own line. The deposit row is an ordinary row: label, editable price, When.
  The Terms card loses its Deposit field and its note reads "Rent and start." (§9 Q1).
- **Summary card.** "Move-in charges" and "First invoice" count only `with_invoice` rows (as today
  for the deposit); each other priced row is listed under them: "Sign fee · $500.00 · due Dec 15,
  2026", "Security deposit · $3,000.00 · 3 payments from Dec 1, 2026".
- **Lease page.** The deposit tile keeps `deposits.card_text`'s words. Each setup charge row in the
  lease's charges list shows the same words for a dated charge not on an invoice ("due Dec 15,
  2026") and for a charge on a plan ("1 of 3 installments paid"); a charge on the first invoice
  shows what it shows today.
- **Errors:** field errors under the row; the service's refusal as one sentence naming the charge.
  No new route, no new permission (the page stays billing-only), no new audit action (the plan and
  the charge are audited as today).

## 5. Work breakdown (Opus; a commit per task; tasks in order, all touch `forms.py` or the page)

1. `services/charge_timing.py`: the type, the labels, `create_lease`; `deposits.py` loses its
   timing section; `installments.lease_channel_collection` and the "payments" preview sentence;
   `views/leases.py` calls it. Done when the 015d deposit tests pass with only names re-pinned.
2. The form: per-row When fields, validation, `timings()`, `setup_items()`, `summary()` with the
   per-charge lines; the `deposit_*` and `*_due_date` fields removed. Done when a POST of each of
   the three choices on a non-deposit row saves the charge, the plan and the draft as §4 says.
3. The page: the row, the deposit as an ordinary row, the Terms card without Deposit, the script
   (enable on price, show the sub-fields, the live preview and Summary), the styles and the phone
   block. Done when the page works without script from a re-render and with it live.
4. The lease page's per-charge words.
5. Baselines regenerated on PostgreSQL in the Playwright image, sqlite pixel-identical;
   self-review and report (what was built, skipped, uncertain, not tested).

## 6. Test plan (must pass; written before the build)

- **Unchanged behaviour:** a form posted with no When field at all saves exactly what main saves
  (charges, due dates, `dated=False`, the draft's lines and total) — compared field by field; an
  existing lease with a dated deposit and one with a deposit plan read the same on the lease page.
- **Happy paths, deposit and a non-deposit row each:** with the first invoice; due on a date
  (charge off the draft, `dated`, due date, a receivable by that date); in installments (plan,
  count, dates, amounts summing to the price, the last carrying the remainder, off the draft).
- **Mixed:** three rows with three different choices on one lease; the Summary's figures; the
  first invoice's total equals rent plus only the `with_invoice` rows.
- **Boundaries:** count 1 and 13 refused, 2 and 12 accepted; a date of today accepted, yesterday
  refused; first date before the start date refused; a price of 0.01 in 12 installments refused by
  the engine's own rule with the row named; a prospective lease; a lease without an operator
  choosing installments (refused, nothing saved); the select disabled without a price.
- **Errors:** a When with no price is ignored; "Due on a date" without a date; installments without
  a count or first date; a refused plan rolls the whole lease back (no lease, no charge, no draft).
- **Adversarial (Fable):** a posted `<name>_when` outside the three values; When fields posted for
  a fee type not offered; an `installments` choice posted with a tampered count of 9999; the same
  creation key replayed (one lease, one plan per charge); a row hidden by "+ Add item" but posted
  with a When; script and markup injection in the date and count fields; the page for a viewer
  (403); the JS and server preview sentences equal over a table of amounts and counts.
- **Reminders and reports pinned:** a dated non-deposit charge enters the ladder by its date; an
  installment by its own; P&L and Revenue totals for a lease are the same whichever When is chosen.
- **e2e:** type a price, the select enables; choose installments, the preview appears; save; the
  lease page shows the words. **Visual:** New lease at both widths and themes.
- New adversarial files run on PostgreSQL before the Publish Request.

## 7. Risk register

- **Billing:** a charge taken off the first invoice is collected only by its own path. For a
  non-deposit charge "Due on a date" there is no invoice and no debit: staff record the payment or
  put it on an invoice later (§9 Q5). This is the main product risk.
- **Installments at save:** the bank account is usually not linked yet when the lease is created,
  so the plan falls back to "manual", as the deposit plan does today.
- **Shared code:** `deposits.py` and `installments.py` are used by lease-end, transfer, Change
  start date and reminders; every consumer's tests run in the full gate.
- **The reference design** `new-lease-dark-laptop.png` shows Deposit under Terms; this plan departs
  from it on the owner's instruction.
- **Removing "More → Due date"** changes a field staff may have used; its replacement behaves
  differently (the charge leaves the first invoice).
- **Not irreversible:** no migration, no stored value rewritten, no processor call added.
- **Secrets:** none touched; no new dependency.

## 8. Acceptance criteria

1. The Security deposit row has an editable price input; no element with id `deposit-mirror` exists.
2. Every row has a When select with the three labels, disabled while the row's price is empty.
3. A POST with no When field produces database rows equal to main's for the same input.
4. Each choice on a deposit and on a non-deposit row saves what §4 states, in one transaction.
5. The move-in draft holds only `with_invoice` charges.
6. The Summary and the lease page show the per-charge words of §4.
7. At 390 px the select is on its own line under the price; no horizontal page scroll.
8. No file under `migrations/` changes; `grep -r deposit_when backend/ledger` finds nothing
   outside tests' history notes.
9. Every §6 case exists and passes on sqlite and PostgreSQL; the full gate is green; coverage of
   the changed files does not drop; the secret scan is clean.

## 9. The owner's decisions (2026-11-13)

1. **The deposit is typed in step 5 only**, as an ordinary row; the Terms card loses its Deposit
   field. Edit lease and every other reader keep working from the same stored value (the deposit
   charge's price; nothing about its storage changes).
2. **"More → Due date" is removed.** A lease that already stores a due date there keeps it, and the
   lease page shows it as "due <date>" (the words of "Due on a date"): a setup charge that is owed
   and whose stored due date is not the lease's start date reads so, with no data rewritten.
3. **Cadence stays on every row:** monthly (default), every two weeks, weekly.
4. **Installments of every charge are collected by the deposit's rule:** bank debit when the
   lease's account is linked, otherwise recorded by staff.
5. **A non-deposit charge "Due on a date" has no invoice of its own** in this PR. The page says so
   beside the choice, on non-deposit rows: "Not on the first invoice. Record the payment or add it
   to an invoice when it is due." Backlog item B10: create a one-charge invoice automatically on
   the due date.
6. **Changing the start date never moves a move-in charge by itself.** The "Move the deposit
   schedule too" tick is off by default (it is today: an unticked box; pinned by a test) and the
   deposit moves only when staff tick it. Other dated charges and plans stay on their dates; staff
   change them by hand. No backlog item. Fable's reading, stated to the owner: a charge billed
   "With the first invoice" has no date of its own and goes where that invoice goes, as today.

## 10. Requirement 7 (owner, 2026-11-13): collecting before the lease starts

- **Dates are free of the start date.** "Due on a date" and an installment plan's first date may be
  any date from today on, also before the lease's start. The rule `deposits.FIRST_BEFORE_START`
  (form and service, PLAN-015d) is removed; its 015d tests are re-pinned to "accepted". No other
  validation refers to the start date.
- **A Prospective lease's move-in charges are invoiced, debited and paid as an active lease's.**
  No new status rule is added anywhere in this PR; the probe of today's behaviour
  (`tests/test_plan015j_today_probe.py`, kept as regression tests) pins: the first invoice sent
  before the start; a payment recorded on a setup charge before the start; reminders on a dated
  charge; a plan's bank debit when the account is linked.
- **Activation is independent.** `activate_due` and "Activate now" do not read the move-in charges
  and bill none of them again (pinned: the counts of rent and setup charges before and after).
- **Postponing the start after a charge is paid or partly paid** leaves the charge, its due date
  and its payments as they are (pinned for a dated charge, for a plan with one installment paid,
  and for a charge on a paid first invoice).

Added to §6: every case above on a prospective lease; a date of today and a date before the start
for both "Due on a date" and the first installment, on a deposit and a non-deposit row.
Added to §8: 10. A first installment date and a due date before the lease's start are accepted.
11. The probe file's tests pass unchanged before and after the build, except the two that pinned
`FIRST_BEFORE_START`, which are re-pinned to acceptance.

## 11. The probe's two conflicts and the owner's decisions (2026-11-13)

The probe (`backend/tests/test_plan015j_today_probe.py`, 18 tests, 28cdebc) showed requirement 7
already holds today except in two places.

- **A. The store page's "Record a payment" refuses a Prospective lease**
  (`payments_entry.NO_LEASE`, through `lease_end.current_lease`: active or paused, started by
  today). **Owner: open it.** Contract: `payments_entry.current_lease(store, today)` returns the
  store's current lease as today and, **only when there is none**, the store's upcoming prospective
  lease not ended (the rule of `workspace.upcoming_lease`: the earliest start first).
  `lease_end.current_lease` itself is not changed, so End lease and the transfer wizard read what
  they read today. A store with a current lease and a prospective successor keeps the current
  lease on that page; the successor's charges are paid from the charge's own form (Fable's
  decision; recorded for the owner). Work: task 6, `services/payments_entry.py` only (plus the view
  if it repeats the rule). Tests: the store-page entry lists and records, in part and in full, a
  prospective lease's dated deposit, its installment and a charge on its unsent draft; a rent
  charge of a prospective lease that exists (its first invoice sent) is payable too; the probe's
  refusal test is re-pinned to acceptance; **lease-end and transfer exactly as before**: End lease
  is not offered for and refuses a store whose only lease is prospective, the transfer wizard's
  source and target rules are unchanged, and `lease_end.current_lease` returns `None` for it
  (new tests asserting the same results on main and on the branch, plus the two suites unchanged).
- **B. Postponing the start with a sent, partly paid first invoice** re-prices the rent charge and
  leaves the sent invoice as it was. **Owner: out of this PR**; BACKLOG B11, "must be fixed before
  go-live"; the owner gives the rule when it is planned. The probe test that pins today's
  behaviour stays as it is.

**Reading confirmed by the owner:** a charge billed "With the first invoice" has no date of its own
and follows that invoice when the start changes; rule 6 covers dated charges and plans only;
charges paid or partly paid are never touched.

Added to §3 In: `services/payments_entry.py`. Added to §5: task 6 (A), after task 2 and before the
page tasks. Added to §8: 12. The store page's Record a payment lists a prospective lease's open
charges when the store has no current lease. 13. `git diff main -- backend/ledger/services/
lease_end.py backend/ledger/services/transfer.py` is empty.

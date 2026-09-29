# Owner Answers — Round 2 (written confirmation)

**Source:** the owner, in writing, September 29, 2026. These are **owner statements**, not data-derived findings. They supersede the STRONG/PARTIAL entries in `ANSWERS_ALL_25.md` for the questions listed, and they close several items in the amendments' blocking list.

**Numbering:** Q-numbers follow the Claude Code open-questions list. Items A–E are the follow-up questions raised while confirming Q6.

---

## Q1 — Positive month value = amount collected that month
**CONFIRMED.** The cell is coloured green.

## Q2 — Negative month value = that month's rent not collected
**CONFIRMED.** The cell stays negative and red until the arrears are collected; on collection it turns positive and green.

**Design consequence:** cell colour carries no information beyond the sign of the value. Q11 is therefore closed: nothing is lost by ignoring colours at import, and the app derives the same colours from `rent_charges.status`.

## Q3 — Positive value different from RENT
**CONFIRMED, with three causes:**
1. **Short payment** (operator paid less than the rent).
2. **Discount or credit** granted by the business.
3. **Cross-store balancing:** some operators have several stores. If one store was over-debited, the business debits another of that operator's stores by less to even it out.

**Owner requirement, verbatim in spirit:** *"It is very important that we can attach a note to the cell where the rent amount is written."*

**Rent history:** not recorded anywhere; the sheet overwrites RENT. Confirmed as a gap the app must close (`lease_rent_history`).

## Q5 — Rec. Date = day of month the rent is debited
**CONFIRMED.**

## Q6 — Ins / Sign / Deposit or Access
**CONFIRMED with an important split:**
- **Sign** and **Deposit/Access** are **one-time** charges. Positive (green) = paid, negative (red) = still owed.
- **Ins (liability insurance) is NOT one-time. It is annual.** Negative = the current year's premium is unpaid. Staff track which years are paid by writing a **cell note** such as "2025 → paid, 2026 not paid".

### Follow-ups on insurance

**A.** Sign and Deposit/Access: one-time, same sign convention as above. Insurance: annual. — *confirmed*

**B. Amount:** currently **$695 per year**; it changes year to year (**$595 last year**). A value like **$1,130 in the Ins column is the sum of two years' payments** (e.g. 595 + 535), because the sheet has only one cell per store. → The app must store an **insurance premium per policy year**, not per store, and never show a multi-year sum in one field.

**C. Channel:** collected by **ACH, the same way as rent**; for most stores it is an **annual auto-payment**.

**D. Closure and open date:** no insurance is charged after a store closes. **Store open date becomes a mandatory field in the app.** Stores currently marked "TBD" will be entered manually by staff.

**E. Short payments:** the shortfall **must be collected the following month**; it is never silently forgiven. If a credit or discount was granted instead, that is recorded as a **note**.

## Q11 — Cell colours
**CLOSED by Q1/Q2:** green = positive, red = negative. Derived, not independent.

---

## New facts that change Prompt 4's schema

1. **Cell notes exist in the sheet and hold real data.** Google Sheets hover-notes on individual cells record insurance year status and rent adjustments. They were **not** in any text/CSV export I made. They must be extracted from an `.xlsx` export (notes survive as comments) before migration, and each note lands on the corresponding `rent_charges` or `store_balances` row.

2. **Per-charge notes are a requirement**, not a nicety. Every `rent_charges` row needs a free-text `note` (author, timestamp), and the month-grid cell must show that a note exists.

3. **Cross-store balancing** is a first-class operation: a credit moved from one lease's charge to another lease's charge, both belonging to the same operator, with a mandatory note. Model it as a `charge_adjustments` row (`from_charge_id`, `to_charge_id`, `amount`, `reason`, `note`, `created_by`) so the month grid still sums correctly and the audit trail is explicit.

4. **Short payments carry forward.** `rent_charges` needs `amount_due`, `amount_paid` and a derived `balance`; a `partial` status; and the next month's run must show the outstanding balance alongside the new charge. Forgiveness is only via an explicit `discount`/`credit` adjustment with a note.

5. **Insurance is an annual recurring charge with its own schedule** (see algorithm below), a **per-year premium table**, and a **per-year paid/unpaid status**. The sheet's single Ins value is an opening balance only.

6. **`stores.open_date` is NOT NULL** for any store that has an active lease; the import puts "open"/"TBD"/malformed values into `open_date_raw` and flags the store `needs_open_date = true`, which blocks insurance scheduling (not rent) until staff fill it in.

---

## Insurance algorithm (approved by owner)

**Inputs**
- `store.open_date` (mandatory), `store.closed_at` (nullable)
- `insurance_premiums(year, amount)` — a table of the annual premium per policy year (2025: 595.00, 2026: 695.00, …), editable by admin
- `payments` of kind `insurance` with `policy_year`, `status ∈ {pending, settled, returned, voided}`
- `today`

**Definitions**
- The **anniversary** for year Y is `open_date` with the year replaced by Y. If `open_date` is Feb 29 and Y is not a leap year, use Feb 28.
- Year Y is **in scope** for a store when `anniversary(Y) ≤ today` and (`closed_at` is null or `anniversary(Y) < closed_at`).

**Charge generation** — run nightly and on demand:
```
for each store with an active or ended lease:
    for Y from year(open_date) to year(today):
        if Y is in scope and no insurance_charge exists for (store, Y):
            create insurance_charge(store, policy_year=Y,
                                    due_date=anniversary(Y),
                                    amount=insurance_premiums[Y])
```
- If `insurance_premiums[Y]` is missing, create the charge with `amount = null` and flag `needs_premium`; do not debit.
- The first year Y = year(open_date) is the "1st year insurance" already charged at setup (Payments Balance tab); on migration mark it paid if the sheet says so.

**Status per (store, year)**
```
settled = any payment where kind=insurance and policy_year=Y and status=settled
status(store, Y) =
    'paid'    if settled
    'pending' if a payment for Y is pending and none settled
    'unpaid'  otherwise   (includes: no payment; payment voided; payment returned)
```
A **voided or returned** payment is treated exactly like no payment: the year goes back to `unpaid` (red). This is the owner's explicit requirement.

**Ins indicator on the store row (replaces the sheet's single cell)**
```
unpaid_years = [Y for Y in scope if status(store,Y) == 'unpaid']
if unpaid_years:  show −(sum of amounts for unpaid_years), red
else:             show +amount of the latest year, green
hover / expand:   "2024 ✓ 595   2025 ✓ 595   2026 ✗ 695"
```

**Collection**
- Insurance is debited by ACH like rent. For stores enrolled in Stax autopay, create a **separate annual scheduled invoice** with `due_date = anniversary`, not a line on the monthly rent invoice, so the two can be paused, refunded and reconciled independently.
- The monthly run view lists insurance debits due in the coming 7 days alongside rent.

**Migration of the sheet's Ins column**
- Import the raw signed value as `store_balances.ins_opening` for reference.
- Parse the cell note (from the `.xlsx` export) for patterns like `2025 -> paid`, `2026 not paid`; create `insurance_charges` for each year mentioned with the parsed status and `legacy = true`.
- If a positive value equals a **sum of two premiums** (e.g. 1,130 = 595 + 535) and the note names two years, split it into two paid years.
- Anything unparseable → flag `needs_review`; do not guess.

**Edge cases to test**
- Open date Feb 29; open date in the future (no charges); store closed one day before its anniversary (no charge); premium missing for a year; payment voided after being counted paid; two payments for the same year (over-payment → credit adjustment); "TBD" open date (no schedule, flagged).

---

## Still open

- **Q4** (is "pending" started in Stax?) — partially answered by C: insurance and most rent are Stax auto-payments; keep as owner-confirmed for insurance, pending for rent.
- **Q7** (channel for the 453 unmarked stores), **Q8**, **Q9**, **Q10**, **Q12**, **Q13**, **Q14**, **Q15**, **Q16**, **Q19**, **Q20**, **Q21**, **Q22**, **Q24** — unchanged.
- **Q25** (ACH limit) — with Stax via the manager.
- **Cell notes** — extraction pending an `.xlsx` export.

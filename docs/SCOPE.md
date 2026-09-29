# SCOPE.md — MVP scope for the Rents ledger

**Status:** NOT ready for sign-off. Corrected 2026-09-29: questions 1, 2, 3, 5 and 6 are pending owner confirmation in writing; the import cannot be written until they are confirmed. Other questions are closed by data or by the owner adopting the recommended handling on 2026-09-27. Nine owner/Stax actions remain (§9, end); three of them gate go-live, none gates the next build prompt.
**Date:** 2026-09-16
**Inputs:** `docs/inputs/RENTS_AUDIT.md` (Rents tab, 534 rows × 142 columns, exported 2026-09-10) and
`docs/inputs/STAX_ANSWERS_public_2026-09-10.md` (public-doc answers; Stax has not replied in writing).
**Scope decision by owner (2026-09-10):** the first version covers the **Rents** tab of the
"Corp Stores" sheet only. The other 15 tabs are out of scope until the owner says otherwise.

This document is the defence against scope creep. Anything not in §3 is not in the MVP.

---

## 1. What the Rents tab is, in one paragraph

The business leases retail spaces inside host stores (mostly Walmart) to about 174 independent
operators running about 517 stores. Each store has a monthly rent (median $3,800, max $12,581) and
a day of the month on which rent is collected. Collection is mostly ACH through Stax, with some
wires. Staff record the outcome per store per month in a grid: the amount collected, a negative
number when it was not collected, "M/D pending" while an ACH debit is in flight, or "Closed". About
450 debits happen a month, clustered on days 1, 2, 5 and 15, and roughly 47 a month fail. Monthly
collections are about $1.5M, roughly $18M a year.

---

## 2. Sensitive data statement

- The Rents tab contains **no card numbers, CVVs, bank account numbers or routing numbers**
  (audit §0, confirmed by the owner on 2026-09-10 for the whole sheet).
- **The sheet's version history has not been checked yet.** Until it is, treat the sheet as
  possibly containing deleted sensitive cells. Checkpoint 1 requires that check before cutover.
- Personal data on the tab is limited to operator nicknames. No emails or phone numbers exist on
  the Rents tab; they must come from elsewhere (question Q16).

---

## 3. The MVP: four flows, mapped to rent collection

The founding prompt fixes the MVP at four flows. Here is what each one means for the Rents ledger.

### Flow 1 — Create and send an invoice = generate the monthly rent charge

- For every lease with `collection_status = active`, on each of its **debit days** (one per month
  for 421 leases; **two per month** for the two leases marked "1 and 15" and "5 and 20"), the app
  creates one **rent charge** in our database (store, operator, period, amount due) and one
  **Stax invoice** for it. Twice-monthly leases split the monthly rent into two equal charges
  unless the owner says otherwise (Q28).
- Leases with `collection_status = paused` (two today) get no charge and no invoice until staff
  resume them. Leases with no debit day (92 today) are treated as paused with `needs_review`.
- The Stax invoice is the vehicle for collection. How it is collected is **Decision D1 (§4)**,
  which the owner must make. The app supports exactly one of the D1 options in the MVP.
- Staff can also create a **one-off charge** for an existing lease (catch-up, adjustment) with a
  free amount and a reason. This covers the "positive value ≠ rent" cases without a formula.
- Wire-channel leases get a rent charge but **no Stax invoice**; they are settled manually (Flow 2).

**Out of MVP:** one-time fees (insurance, sign, deposit/access), termination invoices, late fees,
discount rules, proration for mid-month openings or closings. See §6.

### Flow 2 — Record payments: automatically from Stax, manually for cash/check/wire

- **Automatic:** webhooks `create_transaction` and `update_transaction_settled` (verified against
  the Stax API by re-fetch, per `docs/CLAUDE.md` Rule 4) create and update a **payment** row:
  `pending` when the ACH transaction is created, `settled` when `settled_at` is populated,
  `returned` when the transaction is voided with an ACH reason code. The nightly reconciliation
  job repairs anything webhooks missed.
- **Manual:** staff record a wire, check or cash payment against a rent charge with amount, date
  and a reference. For charges that have a Stax invoice, the app also calls Stax's "mark invoice
  paid in cash or check" so Stax's invoice status stays the source of truth (TODO-VERIFY-016 on
  whether wire is an accepted method there).
- A **returned** ACH payment leaves the charge unpaid and shows the Stax/Nacha reason code. Staff
  re-initiate by creating a one-off charge or resending the invoice (D1-dependent).

### Flow 3 — See who owes what

- **Month grid:** stores × months, the same shape staff use today. Cell states: collected (amount),
  unpaid (negative amount), pending (with start date), partially paid, closed, not due.
- **Arrears list:** every lease with unpaid charges, count of unpaid months, total owed, last
  payment date, operator, payment channel, sortable and filterable by operator and state.
- **Run view for a day:** every charge due today, grouped by status (created, sent, pending,
  settled, returned, not created because lease inactive).
- **Store detail:** lease, rent history, charges, payments, notes.

### Flow 4 — Export CSV

- Export the month grid (stores × months) as CSV in the same column order as the sheet, so staff
  can diff it against the spreadsheet during the parallel run.
- Export the arrears list and the payments list for a date range.
- No other report formats. No scheduled emails of exports.

---

## 4. Decision D1 — how monthly rent is collected through Stax (DECIDED: D1-B, owner, 2026-09-27)

The founding prompt assumed customers pay each invoice on Stax's hosted page. The audit shows the
business today **debits operators' bank accounts by ACH** on a fixed day, without operator action.
Both can be done without bank data touching the app, but they are different products for the
operator. Pick one for the MVP.

| Option | How it works | Operator experience | What changes in our rules |
|---|---|---|---|
| **D1-A: Hosted invoice, operator pays** | App creates a Stax invoice per store per month and Stax emails it. Operator pays on the Stax page (ACH or card, configurable per invoice). | Operator must act every month. Different from today. | Nothing. Matches `docs/CLAUDE.md` as written. |
| **D1-B: Stax scheduled invoice with autopay (recommended)** | Operator pays the **first** invoice on the Stax hosted page and saves a bank method there (Stax stores it; we only see a token ID). App then creates one **Stax invoice schedule** per lease (`FREQ=MONTHLY`, `payment_method_id` set). Stax generates and autopays each month. We read results by webhook. | Same as today: automatic debit on the debit day. One-time enrolment step. | Rule 2 list of allowed write calls grows by "create/edit/pause invoice schedule". Rule 1 unchanged: the bank method is captured on Stax's page, never by us. |
| **D1-C: App-initiated pay with stored method** | Same enrolment as D1-B. App creates each month's invoice itself and calls `POST /invoice/{id}/pay` with `payment_method_id` and an `idempotency_id`. | Same as today. | Rule 2 currently **excludes** `POST /invoice/{id}/pay`; it would need to be re-admitted for token-only use. More control (pacing, retries) but the app carries the run. |

**Decision: D1-B (owner confirmed 2026-09-27).** It preserves the operators' current experience, keeps every debit
attempt inside Stax where the per-transaction limit, ACH rules and retries live, and keeps our
app read-mostly. D1-C is the fallback if scheduled invoices turn out not to support what we need
(TODO-VERIFY-012: autopay on ACH, per-schedule amount edits, behaviour on a returned payment).

**Facts verified in the Stax docs on 2026-09-16 (see `docs/TODO-VERIFY.md` for the open parts):**
- Scheduled invoices support autopay via `payment_method_id`; without it "the customer can manually
  pay the invoice, and optionally save a payment method for future use." (Scheduled Invoices page)
- `POST /invoice/{id}/pay` takes `payment_method_id` and `idempotency_id`. (Pay an Invoice page)
- Per-invoice choice of ACH only, card only, or both. (Configuring Payment Methods for Invoices)
- ACH shows `success: true` immediately, batches at 8 pm ET, then settles or is voided within about
  5 business days; `update_transaction_settled` fires either way; a rejection carries a Nacha reason
  code in `child_transactions[0].meta.achDescription`. (ACH Transactions page)
- "Create a Bank Payment Method" exists in the API but takes raw account and routing numbers. **The
  app will never call it** (Rule 1). Enrolment happens on Stax's hosted page only.

**Either way, these hold:**
- One Stax customer per **operator**. One Stax invoice per **store** per debit so the grid stays
  store-level. **Settled by the data (Q17):** 93 operators hold more than one store, 56 of them
  with different debit days, and the largest holds 47 stores across 8 debit days for $172,002 a
  month. A combined invoice has no single run date and would exceed any plausible limit.
- Existing operators already paying through Stax likely already have Stax customer records and
  stored bank methods (39 Rents rows say STAX). Migration maps them by ID from a Stax customer
  export; it never re-enters bank details and never matches on nickname alone. Question Q19.

**Two caveats folded into D1-B (Round 1, 2026-09-16):**
1. **Enrolment is a campaign, not a code task.** Every operator must open a Stax hosted page once
   and save a bank method. That needs an email per operator, and the sheet holds emails for about
   a dozen of ~174 operators (Q16). Plan a **store-by-store parallel run**: the current ACH method
   keeps collecting for a lease until that lease's operator has enrolled, and the app tracks
   `enrolment_status` per operator. There is no single cutover date.
2. **Not every lease is one monthly schedule.** Two leases bill twice a month, two are paused, and
   92 have no debit day. The app owns pause, resume and multiple debit days per lease. Under D1-B
   that means up to two Stax invoice schedules per lease and staff-driven pause/resume of them.

---

## 5. Data model (names fixed here so every later prompt uses them)

| Table | Purpose | Key fields |
|---|---|---|
| `stores` | One physical space | App-generated primary key. `external_label` (column A verbatim, **non-unique**: "2475" occurs twice for two different spaces, "MALL" ten times), `store_number` (integer derived from the label when numeric, non-unique), `city`, `state`, `open_date`, `open_date_note`, `status` |
| `operators` | The tenant business | `name` (nickname today), `legal_name`, `email`, `phone`, `status`, `enrolment_status` (`not_contacted`, `invited`, `enrolled`, `declined`). Linked to the Stax customer through `external_refs`. |
| `leases` | Store × operator × terms | `store_id`, `operator_id`, `debit_days` (list of 1–2 integers 1–31; empty when unknown), `collection_status` (`active`, `paused`, `prospective`, `ended`), `review_reason` (`wire`, `dormant`, `closed`, `unknown`, NULL), `payment_channel` (`stax`, `wire`, `unknown`, `other`), `allow_card` (bool, default false), `started_on`, `ended_on`, `end_reason` |
| `lease_rents` | Rent history | `lease_id`, `amount_cents`, `effective_from`, `effective_to`, `source` (`sheet_current`, `sheet_inferred`, `staff`) |
| `rent_charges` | One per lease per period per debit | `lease_id`, `period` (YYYY-MM), `seq` (1 or 2; 2 only for twice-monthly leases), `due_on`, `amount_cents`, `kind` (`rent`, `adjustment`, `catch_up`, `waived`), `workflow_status`, `stax_status` (cached), `local_ref`. Grid cell shows the sum over `seq`. |
| `payments` | Money received or attempted | `rent_charge_id`, `amount_cents`, `method` (`stax_ach`, `stax_card`, `wire`, `check`, `cash`), `status` (`pending`, `settled`, `returned`, `voided`), `initiated_on`, `settled_on`, `return_code`, `reference` |
| `store_balances` | Opening one-time balances from the sheet | `store_id`, `kind` (`insurance`, `sign`, `deposit_or_access`), `amount_cents`, `sign_convention_confirmed` (bool) |
| `notes` | Free text with author and date | `entity_type`, `entity_id`, `body`, `author`, `created_at`. Body passes the Rule 1 card/account-number validator. |
| `external_refs` | Stax IDs | per `docs/CLAUDE.md` Rule 8 |
| `provider_snapshots` | Trimmed Stax payloads with `fetched_at` | per Rule 8 |
| `import_audit` | Every sheet cell that became a row, with its original text | `sheet_row`, `sheet_col`, `raw_value`, `target_table`, `target_id`, `rule_applied`, `needs_review` |

The `import_audit` table is not optional. It is how the parallel run proves the app matches the
sheet cell by cell.

---

## 6. Phase 7 — Later (explicitly out of the MVP)

Everything below was seen in the audit or suggested along the way. None of it is built until the
owner signs a new scope.

- One-time fees: insurance ($565/$595/$695/$1,130), sign fees, deposit or access fees, and their
  collection. MVP only stores opening balances from columns H–J.
- Termination invoices and settlements.
- Late fees, discounts, "half rent" rules, proration for partial months.
- Automatic rent escalations or rent change schedules.
- Reminders, dunning emails, SMS, a customer/operator portal.
- Automatic retry of returned ACH payments.
- Card payments as a default channel (ACH only in MVP unless the owner says otherwise, Q22).
- Zelle and any channel other than Stax ACH, wire, check, cash.
- Integration of the other 15 tabs (insurance tracker, audits, IFX invoices, WM rent payable, etc.).
- Accounting export (QuickBooks), deposit reconciliation against the bank statement.
- Multi-user roles beyond "staff" and "admin".
- Migrating months before March 2025 (unless Q12 says yes).
- Cell colours from the sheet (unless Q13 says they carry meaning).

---

## 7. Volumes and limits the MVP must respect

| Item | Value | Consequence |
|---|---|---|
| Active leases | ~517 stores, ~450 debits/month | Day-1 batch is 166 stores. At the 60 req/min steady rate in `docs/CLAUDE.md` Rule 3, creating 166 invoices takes about 3 minutes. Fine. |
| Largest single rent | $12,581 | **Blocker for go-live, not for this step.** Stax's per-transaction ("High Ticket") limit for this account is unknown. Placeholder $2,000 would reject most rents. See `docs/STAX_ANSWERS.md` Q2. |
| Failures per month | ~47 ACH returns | Returns arrive by webhook days later; they are not API failures, so they do not count against the 10-failed-requests/minute rule. They may count against Stax's separate 50-failed-transactions/hour merchant limit (TODO-VERIFY-014). Spread D1-C debits across the day; D1-B leaves pacing to Stax. |
| Stax API limits | 200 req/min and 10 failed req/min per IP, 1-hour block (API Throttling page); a separate page says 100/min with 429 | Design to 100 and 10. TODO-VERIFY-001 narrowed. |

---

## 8. Assumptions made to write this document (each is a question in §9)

A1. Positive month values are amounts collected; negative values are that month's rent not collected. **Assumption, data-derived STRONG, pending owner confirmation (Q1, Q2).**
A2. "M/D pending" is an ACH debit started on that date and not yet settled.
A3. Rec. Date is the day of month the debit runs. **Assumption, data-derived STRONG, pending owner confirmation (Q5).**
A4. Current RENT is the current amount; the sheet holds no rent history, so history is inferred from
    month values only where a value differs from RENT and repeats for consecutive months.
A5. NOTES "WIRE" means the lease is wire-channel; every other lease with a Stax history is Stax-channel.
A6. Operators are Stax customers today and already have stored bank methods in Stax.
A7. ~~The debit is one Stax transaction per store per month, not one per operator.~~ **Settled by data (Q17).**

---

## 9. Answers

The numbered questions are in the Prompt 1 reply and copied to `docs/COLUMN_MAPPING.md` §6.

### Round 1 — 2026-09-16, derived from the Rents and Bank Accounts CSV exports (not owner statements)

**Settled by the data. Applied to this document and to the mapping. No owner input needed.**

| Q | Answer | Applied where |
|---|---|---|
| Q9 | The 14 non-numeric rows (10 MALL, PLAZA, HEB 749, HEB 387, and "SHAHNAZ") are real, active, high-value stores: rents from $2,000 to $12,581, including the largest in the book. They keep their column A text as a non-unique `external_label`; the app generates its own primary key. Two of them are closed (JENSEN MALL, ARBOR PLACE), one has no operator (PEACHTREE MALL), one is paused (VIERA OUTLET). "SHAHNAZ" (Fort Pierce, FL, operator FOWAD) is a store whose label is a person's name; import it and flag for relabelling. | §5 `stores`; mapping §1 |
| Q10 | Store 2475 is two different spaces at the same host store: CONYERS, GA (opened 11/1/2024, $3,950, day 1) and Conyers (SALON) (opened 5/1/2026, $1,900, no debit day). Import both. Store number is not a key. | §5 `stores`; mapping §1 |
| Q17 | One Stax invoice per store per debit. 93 operators hold more than one store; 56 of those have stores on different debit days; the largest holds 47 stores on 8 debit days for $172,002 a month. | §4 |
| Q20 | No debit day above 25 exists today. Implement the last-day-of-month rule anyway. Rec. Date holds five value types: day 1–25 (421 rows), blank (92), "Paused" (2), "1 and 15" / "5 and 20" (2), and "991" (1, a typo). | §3 Flow 1, §5 `leases`; mapping §2 col G |

**Narrowed. The remaining part is for the owner.**

| Q | What the data says | What remains |
|---|---|---|
| Q16 | Emails exist for only about 12 of ~174 operators. The Bank Accounts tab covers 361 stores; 160 Rents stores have no Bank Accounts row. | **Critical path for go-live.** Where is the real contact list (CRM, phone contacts, signed leases, another sheet, messaging app)? If none, collecting ~174 emails and phones is its own phase. |
| Q19 | 39 Rents rows say STAX and the Bank Accounts tab names STAX as the ACH platform on several rows, so some operators exist in Stax already. | Owner or Stax API must supply the customer export with IDs. |
| Q21 | Data supports D1-B (scheduled invoice with autopay), with the two caveats now in §4. | Owner confirms D1-B and the store-by-store parallel run. |

**Not answerable from any export.** Q13 (colours), Q15 (formulas), Q26 (CSV layout), Q27 (version history) stay with the owner. The current sheet contents are clean of card and bank numbers.

**Confirmed.** Rate limits: design to 100 requests/min, 10 failed/min, one-hour block, plus the 50 failed transactions/hour merchant limit. Monthly run: ~450 debits clustered on days 1, 2, 5 and 15, ~47 returns a month.

### Round 1b — 2026-09-27, further data facts from the owner's answer-sheet preparation (not owner statements)

- **Payment channel is unknown for 453 of 532 rows** (neither STAX nor WIRE in NOTES). The mapping now imports those leases with `payment_channel = unknown` and paused; none is enrolled in a Stax schedule until the owner answers Q7 or names a channel source. **This joins Q16 (contacts) and Q25 (ACH limit) as a go-live blocker.**
- Blank debit day (92 leases): 21 WIRE, 13 no rent, 9 closed, 12 collected in Aug 2026, ~50 unexplained (Q29).
- Twice-monthly leases are stores 3409 LEICESTER and 2629 Northbridge; 3409 shows exactly half rent in Aug 2026 (Q28).
- History before Mar 2025 is 10 cells across 5 stores, including a −$3,800 arrear for store 2922 Tucson in Nov 2024. Recommendation: import everything (Q12).
- Store 2204 FOREST PARK pays a flat $500 a month (new Q30).
- The owner's answer sheet is stored at `docs/inputs/ANSWER_SHEET_round2.md` with a numbering crosswalk; it uses different question numbers from this document.

### Round 1c — 2026-09-27, evidence-based answers to all open questions (data-derived and recommendations, **not owner statements**)

Full text: `docs/inputs/ANSWERS_evidence_2026-09-27.md`. Session numbering below.

| Q | Status | Answer applied | Owner still owes |
|---|---|---|---|
| 1 | STRONG | Positive = collected | one word |
| 2 | STRONG | Negative = not collected; arrears accumulate | one word |
| 3 | PARTIAL | All four causes occur; rent history exists only in the grid, back-filled from step changes (40 stores) with review flags | mid-month proration? |
| 4 | STRONG / OWNER | "M/D pending" = payment in flight (48 cells) | which platform started it |
| 5 | STRONG | Rec. Date = debit day | one word |
| 6 | PARTIAL | One-time opening balances; **sign convention unresolved, not guessed; balance reports blocked** until answered | sign convention |
| 7 | PARTIAL | STAX/WIRE mark the channel; **453 rows unknown = hard gate** | channel source for the 453 |
| 8 | PARTIAL | Auto-debits already run for a subset | fraction automatic; what "pending" is operationally |
| 11 | PARTIAL | Now 63: 18 closed, 16 not open, 10 wire, 3 started Sep 2026, 16 no signal | the 16 |
| 12 | Recommendation | Import all 10 pre-Mar-2025 cells | yes/no; store 2204 flat $500 (Q30) |
| 13 | OWNER | Colours invisible; app status replaces them | colour scheme |
| 14 | OWNER | Roles: admin / billing / viewer | assign Ahmed, Zahir, Fatih |
| 15 | OWNER | Data argues against formulas | check five cells |
| 16 | PARTIAL | ~12 of 174 emails in sheet; Stax export is the likely source; the rest is a campaign starting now | where the rest live |
| 18 | SETTLED / OWNER | B is current in all six "A -> B" rows; three of them terminated June 2026 | transfer dates |
| 19 | OWNER | Export Stax customers before any migration code; match by ID once | do the export, or grant API read access |
| 21 | Recommendation | Confirm D1-B and store-by-store rollout | one word |
| 22 | Recommendation | ACH only; per-lease card exception | one word |
| 23 | Recommendation | Mark wires paid in Stax | accounting treatment acceptable? |
| 24 | Recommendation | Reject partials on hosted page; adjusted charges in app | one word |
| 25 | OPEN (Stax) | Must exceed $12,581; also ask about daily/monthly caps (day 1 ≈ $600k) | Stax reply |
| 26 | OWNER | Mirror sheet layout during parallel run only | one word |
| 27 | OWNER | Version history unchecked; current sheet clean | do the check |
| 28 | SETTLED | 50/50; two charge rows per period, grid shows the sum | — |
| 29 | PARTIAL | 21 wire, 13 dormant, 9 closed, 1 STAX, ~50 unknown; import paused with a review reason | the ~50 |
| 30 | OWNER | Store 2204 FOREST PARK flat $500 | rent or other arrangement? |

**Go-live blockers after Round 1c:** Q7 (channel unknown for 453 leases), Q16 (operator contacts), Q25 (ACH limit). Q6 blocks only the one-time balance reports, not rent collection.

### Round 2 — owner answers

**2026-09-27, owner, in session:** "yes for the first part", recorded at the time as covering Q1, Q2, Q5, Q21, Q22, Q24, Q26 and Q28. **On 2026-09-29 the owner corrected this: Q1, Q2 and Q5 are not owner-confirmed and stay pending until confirmed in writing.** The rows for Q21, Q22, Q24, Q26 and Q28 below are left as recorded and are listed for re-confirmation in the status note of 2026-09-29.

| Q | Owner answer | Effect |
|---|---|---|
| 1 | **Pending owner confirmation** (corrected 2026-09-29) | Data-derived STRONG: positive month value = amount collected. Not an owner statement. |
| 2 | **Pending owner confirmation** (corrected 2026-09-29) | Data-derived STRONG: negative month value = rent not collected; arrears accumulate. Not an owner statement. |
| 5 | **Pending owner confirmation** (corrected 2026-09-29) | Data-derived STRONG: Rec. Date = debit day. Not an owner statement. |
| 21 | Yes | **Decision D1 = D1-B**, Stax scheduled invoice with autopay, with a store-by-store rollout and no single cutover day. **Closed.** |
| 22 | Yes | Stax invoices are **ACH only**; card is a per-lease exception staff switch on. **Settled.** |
| 24 | Yes | **Partial payments are rejected** on the hosted page; agreed partials become adjusted charges in the app. **Settled.** |
| 26 | Yes | CSV export mirrors the sheet layout (A–K + months) during the parallel run; normalised export afterwards. **Settled.** |
| 28 | Yes | Twice-monthly leases split rent 50/50. **Settled.** |

**2026-09-27, owner, in session:** "vault's key is live; as for the remaining, do what was recommended." Owner statements:

- **The Stax key stored in Key Vault `finance-kv` is the LIVE key.** Secret is to be named `stax-api-key-live`. No sandbox key exists yet; one is required before any Stax call is made from development or from the sandbox environment (Checkpoint 0 item, still open).
- The Round 1c recommendations are adopted as decisions:

| Q | Decision (recommended handling adopted by owner) |
|---|---|
| 3 | Rent history back-filled from step changes in the grid, flagged for review. Step changes take effect on the 1st of the first month showing the new value; no proration modelled (Q31 closed the same way). |
| 4 | "M/D pending" imports as a pending payment initiated on that date. Platform assumed Stax only where the store has a STAX note; otherwise `method = unknown`. |
| 6 | Sign convention **not guessed**. Raw signed values imported with `sign_convention_confirmed = false`; every report that sums or interprets these balances stays disabled until the owner states the convention. One-time amounts. |
| 7 | STAX/WIRE notes set the channel. The 453 leases with neither are `payment_channel = unknown` and paused; **none is enrolled or debited until a channel list arrives.** Expected source: the Stax customer export (Q19). |
| 8 | Assume a subset of stores already runs on Stax scheduled invoices. **Migration must list existing schedules and attach to them rather than create duplicates** (new TODO-VERIFY-018). |
| 11 | The 63 Aug-2026 blanks import in the five groups of Round 1c; the 16 with no signal are paused with `review_reason = unknown`. |
| 12 | Import all history including the 10 pre-Mar-2025 cells. |
| 13 | Cell colours are ignored; the app's status fields replace them. Owner may still describe the scheme; if so it is checked against the import, not used by it. |
| 14 / 32 | Roles: `admin` (rent, debit day, channel, schedule changes; audited), `billing` (payments, notes, pause/resume), `viewer`. Assignment of Ahmed, Zahir and Fatih to roles is an owner action at go-live. |
| 15 | Assume no formulas. Owner's five-cell check stays on the checkpoint list; if a formula is found, the import script re-reads through the Sheets API. |
| 16 | Operator contacts come first from the Stax customer export; the remainder is a collection campaign that starts now and runs alongside the build. `operators.enrolment_status` tracks it. |
| 18 | B is the current operator in all six transfer rows; A's lease is ended with unknown date and a review flag; the three rows noted "terminated June 2026" end B's lease on 2026-06-30. |
| 19 | **Owner action:** export the Stax customer list (Stax Pay → Customers → Export) and place it in `docs/inputs/` (emails are personal data: keep it out of git, see note below). Migration matches by Stax ID once, owner-confirmed, into `external_refs`. Live API reads from a development session are not allowed (`docs/CLAUDE.md` §6). |
| 23 | Wire, check and cash payments are also marked paid in Stax through the "mark paid in cash or check" call so Stax stays the single payment ledger (TODO-VERIFY-016 on the method value). |
| 27 | **Owner action:** search the sheet's version history for card and bank numbers before cutover. |
| 29 | The ~50 blank-debit-day leases with no signal are paused with `review_reason = unknown`. |
| 30 | Store 2204 FOREST PARK imports as rent at $500 with a review flag. |
| 25 | **Stax action, unchanged:** ACH per-transaction limit must exceed $12,581; also ask about daily caps. Monthly run does not go live until answered. |

**Note on the customer export:** it will contain operator emails and phone numbers. Store it outside the repository or add `docs/inputs/private/` to `.gitignore` before saving it. It is personal data under root `CLAUDE.md` §5.2.

### Remaining owner and Stax actions (not questions)

1. Create a Stax **sandbox** API key and store it as `stax-api-key-sandbox`, in a sandbox resource group or vault that development identities can reach and the live one cannot.
2. Rename the existing live secret `stax-api-key-live`.
3. Export the Stax customer list (Q19) and keep it out of git.
4. Search the sheet's version history for card and bank numbers (Q27).
5. Click five cells for formulas (Q15) and, optionally, describe the colour scheme (Q13).
6. State the sign convention for Ins, Sign and Deposit (Q6) whenever convenient; nothing in rent collection waits on it.
7. Supply the payment channel for the 453 unmarked leases (Q7), most likely via item 3.
8. Assign Ahmed, Zahir and Fatih to roles (Q32) before go-live.
9. Get Stax's answer on the ACH limit and any daily cap (Q25).

Everything else in this document is decided. It is ready for sign-off (§10) subject to those actions.

---

## 10. Sign-off

Business owner: ______________________  Date: __________

By signing, the owner accepts that only §3 is built, that §6 waits for a new scope document, and
that Decision D1 in §4 is final for the MVP.

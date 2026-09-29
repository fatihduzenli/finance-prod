# Amendments to the Prompt Series — after the Rents-tab audit

**Why this file exists.** The 18-prompt series was written from the founding assumption that *a customer receives an invoice and pays it on a Stax hosted page*. The Rents audit established something different: **the business debits ~500 operators' bank accounts by ACH on a fixed day, without the operator acting each month** (Decision D1, scheduled invoice with autopay).

The eight ground rules in Prompt 0 all still hold, unchanged — bank details still never touch the app. But several prompts describe the wrong flow, and **one prompt puts the MVP's core feature in the Phase 7 backlog.** Apply these amendments as each prompt comes up. Nothing here changes a prompt already completed and signed off except where noted.

**Terminology across all remaining prompts:** where a prompt says *customer*, read **operator**. Where it says *invoice*, read **monthly rent charge for one store**, except where it explicitly means a Stax invoice object.

---

## ⚠️ Prompt 12 — a direct contradiction, fix before Prompt 4

Prompt 12 lists as Phase 7 backlog item 2:

> *Scheduled (recurring) invoices via Stax's scheduled-invoice endpoints.*

**Under Decision D1 this is the MVP, not the backlog.** Rent collection *is* recurring scheduled invoicing. Delete item 2 from the Phase 7 list and record in `docs/DECISIONS.md` that it moved into MVP scope with D1.

This matters now, not at Prompt 12: **Prompt 4's schema and Prompt 5's `PaymentProvider` interface both have to support scheduled invoices**, and neither currently mentions them.

---

## Prompt 4 — schema: four tables missing

Prompt 4 says to keep the listed semantics *exactly*, and its list has no home for a store, an operator, a lease or a monthly rent charge. Add these, keeping every table Prompt 4 already specifies:

```
stores
  id uuid pk, external_label (col A: "2475", "MALL", "HEB 749" — NOT unique),
  city, state, open_date date nullable, open_date_raw text (holds "open", "TBD",
  "hold by WM"), status (prospective | active | closed | terminated),
  created_at, updated_at

operators
  id uuid pk, display_name (the sheet's nickname), legal_name nullable,
  email nullable, phone nullable, notes, archived_at
  -- email is NULLABLE on purpose: only ~12 emails exist today (see Prompt 6)

leases
  id uuid pk, store_id, operator_id,
  rent_amount numeric(12,2), debit_day int nullable,
  second_debit_day int nullable,        -- "1 and 15", "5 and 20"
  collection_status (active | paused | ended),   -- Rec. Date held "Paused"
  payment_channel (stax_ach | wire | other),     -- from NOTES "STAX"/"WIRE"
  start_date, end_date nullable,
  stax_schedule_state (none | invited | autopay_active | failed)

lease_rent_history
  lease_id, rent_amount numeric(12,2), effective_from, effective_to nullable
  -- REQUIRED: the sheet overwrites RENT, so rent history exists nowhere today

rent_charges
  id uuid, lease_id, period_month date,  -- unique(lease_id, period_month, seq)
  seq int default 1,                     -- 2 for twice-monthly leases
  amount_due numeric(12,2), amount_paid numeric(12,2),
  status (due | pending | paid | partial | unpaid | waived),
  legacy bool default false
```

`payments` as specified in Prompt 4 stays, but **add `status` values `pending` and `returned`**, and `returned_at`, `return_reason`. ACH settles over days; a payment is `pending` until Stax reports funded or returned.

`invoices` and `invoice_line_items` are kept as specified — a Stax scheduled invoice is still an invoice object — but a rent charge, not a hand-built line-item document, is what creates one. Keep `external_refs` as the only place Stax IDs live, per rule 8.

**Sign convention still unconfirmed** (owner questions 1–3, 6). Do not encode a guess in a migration. Model `rent_charges.status` explicitly rather than inferring arrears from a negative amount.

---

## Prompt 5 — client: add the scheduled-invoice methods

Add to the `PaymentProvider` interface, verifying each against the spec at `https://docs.staxpayments.com/llms.txt` before writing code:

```
createScheduledInvoice, listScheduledInvoices, updateScheduledInvoice,
deleteScheduledInvoice
listAchRejections(range)        -- or whichever report endpoint the spec exposes
```

ACH returns are not an edge case here: **roughly 47 of ~450 monthly debits fail**, about 13% in the one sampled week. The app needs a first-class way to read them.

Two things to watch, both of which are Prompt 5 STOP conditions already:
- **The shared rate-limit bucket.** ~450 debits clustered on days 1, 2, 5 and 15 means the limiter must be global, not per-instance. On serverless, insist on the queue.
- **Static egress IP.** Checkpoint 13 tells you to ask Stax to whitelist the server IP, but a serverless host generally has no fixed IP. Either run the monthly job and the migration from a host that has one, or drop the whitelisting plan and rely on pacing.

---

## Prompt 6 — operators, and the email problem

Prompt 6's STOP condition *"You need to know how we handle customers with no email address"* fires immediately and hard:

- **Only ~12 email addresses exist in the whole spreadsheet**, across ~174 operators.
- 160 Rents stores have no Bank Accounts row at all.

**Rule to implement:** an operator with no email is a valid, fully usable record — they still have stores, leases, rent charges and arrears — but they are **not eligible for autopay onboarding**, and the UI must say so plainly and count them on the dashboard. Do not block operator creation on email.

Everything else in Prompt 6 (local-first create, check-before-create by normalized email, single manual retry, no automatic loop) applies unchanged to operators.

---

## Prompt 7 — replace "invoice builder" with lease setup + monthly rent run

The indicative payload and the "pick customer, add line items" UI in Prompt 7 do not describe this app's main flow. Replace the task with two flows, keeping every constraint Prompt 7 imposes (exact decimals, validate totals against Stax's response, check-before-create, never write `provider_status`, audit-log everything):

**7a — Lease setup and autopay onboarding**
- Create or edit a lease: store, operator, rent, debit day(s), channel, start date.
- "Invite to autopay": create the Stax scheduled invoice and have **Stax** send the email, so the operator saves a bank method once on the hosted page. Sets `stax_schedule_state`.
- Requires an operator email, so surface the blocker here.

**7b — Monthly rent run**
- A screen listing the leases due today by debit day, each showing last month's outcome.
- Creates one `rent_charge` per lease per period, idempotently on `unique(lease_id, period_month, seq)`, which is the duplicate guard that matters more than `creation_key` here.
- Paced deliberately, through the rate-limited client.
- Handles pause and resume, and the twice-monthly leases.

**Keep Prompt 7's large-invoice warning, and take it seriously:** the largest single rent is **$12,581** and several leases exceed $4,000. Until Stax confirms an ACH per-transaction limit above $12,581, the monthly run **must not go live** (owner question 25).

---

## Prompt 8 — webhooks: add the ACH return path

Keep the whole design: URL token, constant-time compare, 404 on mismatch, store-then-200-then-enqueue, re-fetch by ID, idempotent processing. Add:

- **A payment state machine:** `pending → settled` or `pending → returned`. The sheet's "9/9 pending" cells are exactly this state, and it is the one the current spreadsheet handles worst.
- **On a return:** reopen the `rent_charge` as unpaid, record the reason, raise it into a returns queue for staff. Do **not** retry automatically — a re-debit needs the operator's authorization.
- Subscribe to transaction events plus whatever the spec exposes for ACH returns or deposits, in addition to Prompt 8's list.

The "lying payload" test in Prompt 8 stays as written; it is the most valuable test in the series.

---

## Prompt 9 — wire payments, not just cash and check

The sheet shows **32 stores paying by wire** and no cash at all. Keep the Stax cash/check endpoint work, and add **wire** as a payment method recorded in the app.

**Owner question 23 must be answered before building this:** when a wire arrives for a store that also has a Stax invoice, does the app mark that Stax invoice paid (so Stax stays the single ledger), or stay app-only? Default recommendation: mark it in Stax via the cash/check-style endpoint so reconciliation has one source.

Add one screen Prompt 9 does not have: **a returns worklist** — returned debits, the reason, the staff note, and the re-processing outcome. That is the single most time-consuming manual task today.

---

## Prompt 10 — reconciliation: pull ACH rejections too

Add to the nightly job: fetch ACH rejections for the overlap window and reconcile them against `payments` in `pending`. A `pending` payment older than a configurable number of days with no webhook is a mismatch and must alert — that is precisely the failure this job exists to catch.

---

## Prompt 11 — dashboard: the month grid is a requirement

Prompt 11's warning about "one person's favourite view disappearing" applies directly. Staff think in the sheet's layout, so build it:

- **Month grid:** stores down, months across, colour-coded paid / unpaid / pending. This replaces the Rents tab visually.
- **Arrears list by months behind** (1, 2, 3+) with the total owed. 493 month-cells sit at exactly minus the rent, and most repeat into the next month, so multi-month arrears are the norm, not the exception.
- **Today's run:** debits due, pending, returned.
- Keep Prompt 11's A/R aging, revenue by month, payments by method and CSV exports.

Baseline figures to validate reports against: **Aug 2026 — 392 collected, $1,470,001.57, 47 unpaid, 13 pending.**

---

## Prompt 13 — migration: history is a grid, not an invoice list

Prompt 13's policy is right and gets a concrete shape here:

- **18 months of history (Mar 2025 – Aug 2026)** become `rent_charges` with `legacy = true`, **local only, never pushed to Stax.** This is exactly the "historical fully paid invoices" rule, and pushing them would corrupt Stax's reports permanently.
- `Sep 2024 – Feb 2025` holds 1–3 entries per month: owner question 12 decides.
- "M/D pending" cells import as `pending` payments; "Closed" sets a lease end date.
- **"991" in Rec. Date is a typo** and must be corrected at import, not imported.
- Pre-flight scan stays mandatory, though the Rents tab is already known clean: no card, CVV, account or routing numbers.
- **The Bank Accounts tab is never imported.** Restrict it now and delete it after cutover.

Add a phase Prompt 13 does not anticipate: **the autopay onboarding campaign.** ~500 leases each need the operator to save a bank method once. This is the real critical path, it depends on collecting ~174 emails, and it cannot be scripted.

---

## Prompt 16 — cutover is per operator, not a single date

Prompt 16 assumes one cutover day. Autopay onboarding cannot work that way: each operator migrates when they have saved a bank method.

- Run the **old collection method and the app side by side per operator**, moving stores across as each one is onboarded. The parallel run is therefore staggered, and the two-clean-weeks exit condition applies per cohort, not once globally.
- Keep every live-key safety step exactly as written: `STAX_ENV = live`, banner gone, **production webhook re-registered in the live account**, small real smoke test, reconciliation scheduled.
- Watch the first real **ACH return** specifically, not just the first payment. It could not be simulated in the sandbox and it happens ~47 times a month.

---

## Unchanged and still correct

Prompts 0, 2, 3, 14, 15 and 17 need no amendment. The eight ground rules stand as written, and two are load-bearing here more than the series assumed:

- **Rule 3 (rate limits).** ~450 debits and ~47 failures a month, clustered on four days, sit much closer to the 10-failed-per-minute lockout than one-off invoicing would. Prompt 15's bad-email test is the right test; run it against the real operator list.
- **Rule 1 (no bank data).** The whole point of D1: the operator saves the bank method on Stax's page, and the app only ever holds a token reference.

## Still blocking, carried forward

1. **ACH per-transaction limit** must be confirmed above **$12,581** before the monthly run goes live (question 25).
2. **Operator emails** — ~12 of ~174 exist. Blocks autopay onboarding, so it blocks go-live (question 16).
3. **Owner questions 1–3 and 6** — the meaning of positive, negative and mismatched month values, and the sign convention. The import cannot be written without them.
4. **Sheet version history** unchecked for card and bank numbers (question 27).

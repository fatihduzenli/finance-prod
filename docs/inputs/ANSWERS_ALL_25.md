# Answers to All 25 Open Questions — Evidence-Based Best Answers

**Prepared:** September 27, 2026, from a fresh CSV export of the Rents tab.
**Important:** the live sheet has changed since the September 10 audit. One count moved (Q9: 68 → 63). Re-run the import pre-flight against the sheet as it is on migration day, not against these figures.

**How to read the status column:**
- **SETTLED** — the data answers it. Build on it.
- **STRONG** — the data points clearly one way. Build on it, but the owner should confirm with one word.
- **PARTIAL** — the data narrows it; the remainder needs the owner.
- **OWNER ONLY** — nothing in the export can answer it. My answer is a recommendation, not a finding.

**Ground rule for Claude Code:** every answer below is *data-derived* or a *recommendation*. Record them in `docs/SCOPE.md` under that label, distinct from owner statements. Where a question is marked STRONG or PARTIAL, the owner's one-word confirmation is still required before the import is written.

---

## Section B — What the month cells mean

### Q1. Positive month values are the amount collected for that month?
**STRONG — Yes.**
- Aug 2026: 392 stores hold a positive value, totalling $1,470,001.57.
- Across all months, ~80% of positive values equal the store's RENT to the cent.
- The alternative reading ("amount charged") fails because the same rows switch to negatives and "pending" text in months when collection did not happen, which only makes sense if positive = money received.

### Q2. Negative month values are that month's rent not collected?
**STRONG — Yes.**
- 648 negative cells. **493 equal exactly −RENT.**
- In 540 of them the next month is also negative, i.e. arrears accumulate rather than being re-recorded.
- The remaining 155 negatives that don't equal −RENT are consistent with arrears on a rent that later changed (see Q3).

### Q3. When a positive value differs from RENT — rent change, partial, catch-up or discount? Is rent history written anywhere?
**PARTIAL — All four occur. Rent history is not recorded anywhere.**
- **Rent changes are common:** 40 stores show a permanent step change in their monthly value (e.g. 2,000 → 2,100 → 3,500). 11 NOTES cells say "new rent from 9/1", "Rent increase 9416", etc.
- **Partial payments exist:** 34 stores have at least one value below 60% of rent.
- **Discounts exist:** NOTES include "$500 discount given until we find a new operator".
- **Twice-monthly splits exist:** see Q23 — a half-value cell is not a partial payment.
- The RENT column holds only the *current* rent and is overwritten. **The month grid is the only rent history that exists.** The `lease_rent_history` table must be back-filled from step changes in the grid at import, and flagged for owner review.

*Owner still needed for:* whether any of the 40 step changes were mid-month (proration).

### Q4. "9/9 pending" = a debit started on 9/9 not yet settled? Started in Stax?
**STRONG on the first half; OWNER ONLY on "in Stax".**
- 48 cells match the pattern "M/D pending", all in Aug–Sep 2026, all on stores whose earlier months are plain numbers. The date is always within the month of the column. This is a payment in flight.
- Nothing in the sheet says which platform started it. Given Q7, assume Stax only for stores with a STAX note.

### Q23. Twice-monthly leases: 50/50 split?
**SETTLED — Yes.**
- 3409 LEICESTER, MA: rent $3,700, debit days "1 and 15", Aug 2026 = **$1,850** = exactly half.
- 2629 Northbridge, MA: rent $3,700, debit days "5 and 20"; month cells show the combined total (3,800 → 3,850 after a rent change).
- Model as two `rent_charges` rows per period (`seq` 1 and 2), each at half the rent; the grid cell displays their sum.

---

## Section C — Stores and leases

### Q5. Rec. Date is the day of the month rent is debited?
**STRONG — Yes.** Values are 1–25 and cluster on 1 (166), 5 (71), 2 (49), 15 (37), 10 (27) — a debit-schedule distribution, not a receipt log. NOTES corroborate: "auto payment on 15th", "7th auto payment".

### Q24. The 92 leases with a blank debit day?
**PARTIAL — Mostly "not on an automatic debit".** Within the 92:
- 21 have a WIRE note → channel = wire, no debit day needed.
- 13 have no rent or rent 0 → dormant or prospective.
- 9 have a closed/terminated note.
- Only 1 has a STAX note.
- Only 12 collected anything in Aug 2026.
- **~50 have no explanatory signal.** These are the owner's list.
- Import rule: `collection_status = paused`, with a `review_reason` of wire / dormant / closed / unknown.

### Q9. Stores with a rent but nothing in Aug 2026?
**PARTIAL — Now 63 stores (sheet updated since audit). Four groups:**

| Group | Count | Evidence | Import as |
|---|---|---|---|
| Closed / terminated / sold | 18 | NOTES say so | lease `ended` |
| Not open yet | 16 | Open Date is Sep 2026 or later | lease `prospective` |
| Wire-paid | 10 | WIRE note | active, channel wire |
| Started in Sep 2026 | 3 | Sep 2026 has a value, Aug blank | active |
| **No signal** | **16** | 28 of the 63 have *never* had a month value | owner review |

### Q6. Ins / Sign / Deposit or Access: positive = collected, negative = owed? One-time?
**PARTIAL — Sign convention cannot be settled from this tab; one-time is STRONG.**
- Ins: 253 positive / 66 negative. Sign: 148 / 56. Deposit/Access: 134 / 88.
- NOTES on negative rows and positive rows use the same words ("due", "paid", "discount") at similar rates, so NOTES do not disambiguate.
- Termination Fees tab (from the 16-tab audit) treats negative Sign/Insurance as **credits reducing the total due** — the opposite of "owed".
- **Do not encode a guess.** Import the raw signed value into `store_balances` with `sign_convention_confirmed = false`, and block any balance report that depends on these until the owner answers.
- One-time: these are fixed-amount columns with no month dimension, and the common values (565 / 695 / 1,130 insurance; 3,500 deposit) match the setup fees described on other tabs. Treat as one-time opening balances.

### Q15. "A -> B" operators: is B current? Transfer date recorded?
**SETTLED on "B is current"; transfer date is OWNER ONLY.** All 6 rows:

| Store | Operator cell | Note |
|---|---|---|
| 1580 Indianapolis | ANWAR -> aga | — |
| 2787 Indianapolis | ANWAR -> aga | "$500 discount given until we find a new operator" |
| 4230 Fort Wayne | ANWAR-> asim | "new rent from 9/1 3500 — LEASE TERMINATED IN JUNE 2026" |
| 6476 Danville | ANWAR-> asim | "new rent from 9/1 3600 — LEASE TERMINATED IN JUNE 2026" |
| 5483 Greenwood | ANWAR-> asim | "new rent from 9/1 3600 — LEASE TERMINATED IN JUNE 2026" |
| 2890 Macon | SHAHNAZ -> GP (bought) | — |

- The arrow always points to the newer party; "(bought)" and "until we find a new operator" confirm the direction.
- Five of six involve one outgoing operator (ANWAR), and three of those leases have since terminated.
- Import: lease for B as current; ended lease for A with `end_date = null` and a review flag. Three of the six are then also `ended` per the note.

---

## Section D — Payment channel and Stax usage today

### Q7. NOTES "STAX"/"WIRE" = payment channel? Are all non-WIRE stores debited through Stax?
**PARTIAL — Yes they mark the channel; NO, the rest cannot be assumed Stax.**
- 48 rows say STAX, 32 say WIRE, **453 say neither.**
- NOTES also show "Auto payment stopped", "auto payment on 15th", "7th auto payment" — automatic debits exist, but the platform is named on only ~9% of rows.
- **Import rule: `payment_channel = unknown` for the 453.** A lease with channel `unknown` cannot be enrolled in autopay or included in a monthly run. This is a hard gate, not a warning.
- The owner must supply the channel list from wherever it actually lives (probably the Stax dashboard's customer list — see Q16).

### Q8. Staff start each debit by hand, or does Stax run a recurring schedule?
**PARTIAL — Recurring schedules exist today, at least for some stores.**
- NOTES: "Auto payment stopped b/c of termination", "Auto payment has paused", "auto payment on 15th for 3 months", "7th auto payment".
- Combined with per-store debit days, this reads as **Stax scheduled invoices already running for a subset of stores**, with staff pausing and restarting them by hand.
- Owner still needed for: what fraction is automatic vs manual, and what "pending" means operationally (a scheduled run awaiting settlement, or a manual charge).

### Q16. Can you export the Stax customer list with IDs?
**OWNER ONLY — Recommendation: yes, and do it before any migration code is written.**
- The sheet has no Stax IDs at all. Operator names are nicknames ("CHINO", "ANWAR", "aga").
- Two acceptable routes: (a) the owner exports customers from Stax Pay → Customers → Export (documented feature), or (b) Claude Code pulls `GET /customer` from the sandbox/live account via the read-only API.
- Matching must be **by Stax ID once, confirmed by the owner**, then stored in `external_refs`. Never match by name at run time.

---

## Section E — Decisions

### Q17. Confirm D1-B (Stax scheduled invoice with autopay) and store-by-store rollout?
**Recommendation: confirm both.** The data supports it:
- Q8 shows Stax scheduled/auto payments already exist for some stores, so this is an extension of current practice, not a new mechanism.
- The rollout cannot be a single day because ~500 leases each need the operator to save a bank method once, and only ~12 operators have an email on file (Q14).
- Bank details never touch the app — the Prompt 0 ground rule holds.

### Q18. ACH only, or ACH and card?
**Recommendation: ACH only, with card as a per-store exception the owner switches on.**
- Only 6 NOTES cells mention a card at all.
- Cost: ACH is 1% capped at $10, so a $3,800 rent costs $10. Card-not-present is $0.15 + interchange, roughly $76 on the same rent (≈2% interchange assumed — estimate).
- Stax configures allowed methods per invoice, so the app can default to ACH and expose a per-lease `allow_card` flag.

### Q19. Wire payments: mark the Stax invoice paid, or keep app-only?
**Recommendation: mark it in Stax too, via the "mark paid in cash or check" endpoint.**
- 32+ stores pay by wire (Q7). If those invoices stay open in Stax, Stax's A/R reports and any reminder emails will be wrong for ~6% of the book every month.
- One ledger (Stax) for payment state is the design rule already in `docs/SCOPE.md`.
- Owner must confirm the endpoint's accounting treatment is acceptable (it will appear in Stax as a non-card payment).

### Q20. Partial payments on the hosted page: allow or reject?
**Recommendation: reject, and handle partials through the app.**
- Genuine partials are rare: only 7 cells in Aug–Sep 2026 sit between 30% and 70% of rent, and 2 of those are the twice-monthly halves from Q23.
- Allowing partials on the hosted page would let an operator underpay silently, and Stax's PARTIALLY APPLIED status then has to be reconciled by hand — exactly the workload the app is meant to remove.
- When a partial is agreed, staff record it as a `waived` or `adjusted` charge in the app and Stax bills the reduced amount.

---

## Section F — Migration and working practice

### Q10. Import history from Mar 2025 only, or also Sep 2024 – Feb 2025?
**Recommendation: import everything.** Pre-March-2025 data is 10 cells across 5 stores:

| Month(s) | Store | Value |
|---|---|---|
| Sep 2024 – Feb 2025 (6 cells) | 2204 FOREST PARK | 500 each |
| Nov 2024 | 2922 Tucson | **−3,800** |
| Dec 2024 | 1444 JACKSONVILLE | 3,740 |
| Jan 2025 | 1083 JACKSONVILLE | 3,740 |
| Jan 2025 | 5300 GIBSONTON | 3,831.25 |

- 2922's −3,800 is arrears that would vanish if skipped.
- 2204 at a flat $500/month is far below the $3,800 median — flag for the owner; it may not be rent.

### Q11. Do cell colours carry meaning?
**OWNER ONLY.** The connector returns values only. If colours encode paid/late/closed, that information is invisible to every export I can make and will be lost unless the owner describes the scheme. *Recommendation:* the owner opens the tab and lists which colours are used and what each means; the import then ignores colours, and the app's status field replaces them.

### Q13. Are any month cells formulas?
**OWNER ONLY, but the data argues against it.** Every month value is either a plain number, a negative, or free text like "9/9 pending" or "Closed". Text mixed into a formula column is unusual, and no cell shows a computed pattern (no totals row, no cross-tab references visible). *Recommendation:* owner clicks five cells — one positive, one negative, one "pending", one in column F (RENT), one in column N of Payments Balance — and reports whether the formula bar starts with "=".

### Q12. Who edits the Rents tab? Who may change rent amounts in the app?
**OWNER ONLY.** The sheet's note columns on other tabs name three people — Ahmed, Zahir, Fatih — but the Rents tab has a single NOTES column with no author. *Recommendation:* rent amount and debit-day changes require the `admin` role and an audit-log entry; `billing` can record payments and notes; `viewer` is read-only. Owner assigns the three names to roles.

### Q21. Is the sheet's exact column layout the CSV format staff need for the parallel run?
**OWNER ONLY. Recommendation: yes for the parallel run, no afterwards.** During the side-by-side weeks, a CSV that mirrors A–K plus the month columns lets staff diff the two directly. After cutover, exports should be normalized (one row per store-month).

### Q22. Has the version history been searched for card or bank numbers?
**OWNER ONLY — not yet, and I cannot do it.** The current sheet contains no card numbers (Luhn scan of all 16 tabs) and the Rents tab contains no bank or routing numbers. Older versions may hold deleted data. *Owner action:* File → Version history → See version history; open the oldest few versions and check the Bank Accounts tab and any tab that has since been deleted.

---

## Section A — Blockers

### Q14. Where is the real operator contact list?
**PARTIAL — It is not in this spreadsheet.**
- Bank Accounts tab: 12 rows with an email, across ~361 stores.
- Rents tab: zero emails.
- 160 Rents stores have no Bank Accounts row at all.
- So the sheet holds emails for roughly **12 of 174 operators (~7%)**.
- *Recommendation:* the most likely source is the **Stax dashboard itself** — if operators already receive auto-debits (Q8), Stax has their email. The Q16 customer export would settle Q14 for every operator already in Stax. For the rest, collecting ~160 emails is a campaign that starts now and runs in parallel with the build.

### Q25. Stax ACH per-transaction limit *(Stax only)*
**OPEN.** Must exceed **$12,581** (International Plaza, FL). Secondary check: whether a *daily* or *monthly* cap exists — day 1 alone carries 166 debits totalling roughly $600k. Until confirmed, the monthly run does not go live.

---

## Also outstanding — Azure / Key Vault
**OWNER ONLY.** Two data points for the decision:
1. Stax sandbox and live share `https://apiprod.fattlabs.com`; the key is the only thing separating a test from real money. Label the vault secret with the environment and keep `STAX_ENV` matched to it at boot.
2. IP whitelisting for the migration and the monthly run needs a static egress IP. If the current host cannot provide one and Azure can, that is a concrete reason to move — and it should be decided before Prompt 2 (hosting) is revisited.

---

## Summary table

| # | Status | One-line answer |
|---|---|---|
| 1 | STRONG | Yes — collected |
| 2 | STRONG | Yes — not collected, arrears accumulate |
| 3 | PARTIAL | All four occur; rent history exists only in the grid |
| 4 | STRONG / OWNER | Yes, in flight; platform unknown |
| 5 | STRONG | Yes — debit day |
| 6 | PARTIAL | One-time yes; sign convention unresolved — do not guess |
| 7 | PARTIAL | Yes it's the channel; 453 rows unknown — hard gate |
| 8 | PARTIAL | Auto-debits already run for some stores |
| 9 | PARTIAL | 63 now; 18 closed, 16 not open, 10 wire, 3 started Sep, 16 unknown |
| 10 | Recommendation | Import all 10 pre-Mar-2025 cells |
| 11 | OWNER | Unknowable from export |
| 12 | OWNER | Recommend admin/billing/viewer roles |
| 13 | OWNER | Probably not formulas; owner checks 5 cells |
| 14 | PARTIAL | ~12 of 174 in sheet; Stax export is the likely source |
| 15 | SETTLED / OWNER | B is current; dates not recorded |
| 16 | OWNER | Export from Stax before any migration code |
| 17 | Recommendation | Confirm D1-B and staggered rollout |
| 18 | Recommendation | ACH only, card per-store exception |
| 19 | Recommendation | Mark wires paid in Stax |
| 20 | Recommendation | Reject partials on hosted page |
| 21 | OWNER | Mirror layout during parallel run only |
| 22 | OWNER | Not done; owner checks version history |
| 23 | SETTLED | Yes — 50/50 |
| 24 | PARTIAL | Mostly not on auto-debit; ~50 unknown |
| 25 | OPEN (Stax) | Must exceed $12,581 |

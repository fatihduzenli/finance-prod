# Spreadsheet Audit — "Corp Stores" → **Rents tab only**

**Prepared:** September 10, 2026, from a clean CSV export of the Rents tab (Google Drive connector).
**Scope decision (owner, 2026-09-10):** *The first version of the app covers the **Rents** tab only. All other tabs are ignored for now.* `SHEET_AUDIT.md` (all 16 tabs) is kept for later phases.
Items needing owner confirmation are marked **❓** and listed in §7.

**Limits:** displayed values only, so **no formulas**, no cell colors, no edit history. All counts below come from the full export (534 rows × 142 columns, correctly aligned) and are exact.

**Sensitive data on this tab:** none. There are no card numbers, bank account numbers or routing numbers on Rents. Personal data is limited to operator nicknames.

---

## 1. What the Rents tab is

It is the **monthly rent collection ledger**. There is one row per store (location inside a host store such as Walmart). The fixed columns describe the store, its operator and its current rent; then there is **one column per month** in which staff record what happened to that month's rent.

- **Rows:** 533 data rows: **517** numeric store numbers, **10** "MALL" rows, **5** rows with text in column A ("PLAZA", "STREET", "HEB 749", "HEB 387", and one person's name), **1** blank row.
- **Columns:** A–K fixed (11), then **L–EL = 131 month columns** (Sep 2024 → Jul 2035).
- **Real history starts March 2025.** Sep 2024–Feb 2025 hold only 1–3 entries each. **The last filled month is Sep 2026**, the current month, still in progress. Oct 2026–Jul 2035 are pre-created and empty.

**Scale:**
- The current monthly rent listed across all stores totals **≈ $1.95M**.
- Amounts actually recorded as collected ran **$1.20M (Mar 2025)**, peaked at **$1.62M (Jan 2026)**, and were **$1.47M in Aug 2026**. That's roughly **$18M a year**.

---

## 2. Fixed columns (A–K)

| Col | Header | What it holds (measured) | Kind | Notes / issues |
|---|---|---|---|---|
| A | Store | Store number (517 rows); also "MALL" ×10 and 5 text values | T | **Store 2475 appears twice** ❓. MALL/PLAZA/STREET rows have no number, so they need a generated ID. |
| B | LOCATION, STATE | City name; 18 rows contain "City, ST" | T | Header says "LOCATION, STATE" but it's the city. |
| C | STATE | 2-letter state, 43 distinct; 1 blank | T | |
| D | Operator Name | **174 distinct operators** by name; 11 blank. The largest operator has **47 stores**; the next ones have 18, 18, 15, 14, 13 | T | Nicknames, not legal names. **6 rows show a transfer** written as "A -> B" ❓ who is current. |
| E | Open Date | **494 dates** (1/1/2021 → 1/1/2027); 14 "open", 13 blank, 3 "TBD", 1 "hold by WM", 1 malformed ("2/12025"), 1 with "april" on a second line | T | Mixed types; clean during migration. |
| F | RENT | **Current** monthly rent. 512 rows > 0; min $100, **median $3,800**, max **$12,581**. Most common: $3,800 (88 stores), $4,100 (70), $3,600 (55), $3,700 (43). 14 blank, 1 zero | T | **Only the current rent. There is no rent history** (see §3). |
| G | Rec. Date | Day of month: 1 (166 stores), 5 (71), 2 (49), 15 (37), 10 (27), 7, 20, 3, 25, 6, 9 …; 92 blank | T | ❓ Almost certainly **the day the rent is debited/received**. |
| H | Ins | 319 filled: **249 positive, 69 negative**. Common: 565, 595, 695, 1,130, −695. 1 text ("no sign") | T | ❓ Positive = collected, negative = still owed? |
| I | Sign | 205 filled: 146 positive, 56 negative. Amounts vary (e.g., 1,753.07); 3 text values ("Unknown", "No sign fee") | T | ❓ Same question. |
| J | Deposit or Access | 221 filled: 132 positive, 88 negative. **3,500 (116 rows), −3,500 (67)**; 1 text ("2/10 pending") | T | ❓ Same question. |
| K | NOTES | 240 filled. **Short codes "STAX" (39) and "WIRE" (26)**. Keywords: stax 48, wire 32, close 15, terminat(ed) 14, paid 11, discount 9, half 6 | T | NOTES often records the **payment channel** (Stax ACH vs wire), plus closures, terminations and discounts ❓ |

---

## 3. Month columns (L–EL): what the values mean

Measured across all month cells:

| Value in a month cell | Count / pattern | Most likely meaning |
|---|---|---|
| **Positive number** | e.g., 392 stores in Aug 2026; about 80% equal the current RENT exactly | **Amount collected** for that month |
| Positive but ≠ current RENT | ~1,285 cells: 655 at 0.5–0.99× rent, 543 at 1.01–1.5×, 68 below 0.5×, 19 above 1.5× | The rent **changed over time** (e.g., one store stepped 2,000 → 2,100 → 3,500), **partial** payments, **catch-up** payments or **discounts** ❓ |
| **Negative number** | 648 cells; **493 equal exactly −RENT**; in **540** of them the next month is also negative | **That month's rent was NOT collected**, so arrears build month after month ❓ confirm |
| Text **"M/D pending"** (e.g., "9/9 pending") | 30+ cells, Aug–Sep 2026 | A payment **started on that date and not yet settled** (typical for ACH, which takes days) ❓ |
| Text **"Closed"** | 3 cells | Store closed |
| Blank | — | Nothing recorded: not yet due, store not open or already closed ❓ |

**Current state (Aug 2026):** 452 stores filled; 392 collected, **47 unpaid (negative)**, 13 pending. **68 stores have a rent amount but nothing in Aug 2026** ❓ closed, terminated or not yet open?

**September 2026 is in progress:** 151 stores filled so far (137 collected, 5 unpaid, 9 pending).

---

## 4. The monthly workflow this tab supports (inferred)

1. On each store's **Rec. Date** (day 1, 2, 5, 10, 15 …), rent is collected, mostly by **ACH through Stax** and some by **wire** (NOTES says STAX or WIRE).
2. When a debit starts, staff type **"M/D pending"** in that month's cell.
3. When it settles, they replace it with the **amount**.
4. If it fails or isn't paid, they type the **negative rent**, which stays negative in later months until it is caught up. A later, larger positive may represent a catch-up ❓.
5. They keep an eye on who has several negative months (delinquency).

---

## 5. What the app must do to replace the Rents tab (MVP scope input)

**Data model:**
- `stores`: number (+ generated ID for MALL/PLAZA/STREET/HEB rows), city, state, open date, status.
- `operators`: name (nickname now; legal name and email later); linked to a Stax customer.
- `leases`: store × operator × **rent amount** × **debit day (Rec. Date)** × payment channel (STAX / WIRE / other) × start/end date.
  - **Keep rent history** (`rent_amount` with effective dates), because the sheet overwrites it.
- `rent_charges`: one row per lease per month: period, amount due.
- `payments`: amount, date started, date settled, method (Stax ACH, wire, other), status (**pending / settled / returned**), Stax transaction ID.
- `store_balances`: opening balances for **Ins, Sign, Deposit/Access** (sign convention ❓), kept as simple fields for now.
- `notes`: per store, with author and date (replaces the NOTES column).

**Screens:**
- **Month grid** (stores × months, like the sheet) showing collected / unpaid / pending with colors. Staff already think in this layout, so keep it.
- **Arrears list:** stores with 1, 2, 3+ unpaid months, and the total owed.
- Store detail: lease, rent history, payments, notes.
- **Monthly run view:** today's debits by Rec. Date, which are pending, which failed.

**Stax:**
- Debit each Stax-channel lease on its debit day. Use either a **Stax scheduled invoice with autopay** on the stored bank method, or an app-initiated **charge with an `idempotency_id`** so the same month can never be charged twice ❓ which the team uses today.
- Wire payments are recorded manually in the app; they are not processed through Stax.
- ACH settles over several days, so a payment sits in **pending** until Stax reports it funded or returned.

**Stax transaction limit is a hard blocker for this tab.**
- Single rents go up to **$12,581**, and catch-up payments can be larger. Several stores pay over $4,000.
- The $2,000 placeholder in `STAX_ANSWERS.md` would reject most rents. Get the real **ACH per-transaction limit** before building the monthly run.

**Pace the monthly run.** About 450 debits a month, clustered on days 1, 2, 5 and 15, with roughly 47+ failures a month. Spread debits over the day to stay clear of Stax's reported 50-failed-transactions-per-hour limit.

---

## 6. Migration rules for this tab (draft; confirm §7)

| Sheet value | Becomes in the app |
|---|---|
| Row with numeric store number | `store` + `lease` (operator from D, rent from F, debit day from G) |
| MALL / PLAZA / STREET / HEB rows | `store` with a generated ID; owner confirms each |
| Store 2475 (duplicate) | Owner decides: two spaces, or a mistake |
| "A -> B" in Operator Name | Lease for B (current) and ended lease for A ❓ |
| Positive month value | `rent_charge` for that month **and** a settled `payment` of that amount (assume paid in full unless told otherwise ❓) |
| Negative month value | `rent_charge` of that amount, **unpaid** |
| "M/D pending" | `rent_charge` + `payment` with status **pending**, started M/D |
| "Closed" | Lease end date = that month |
| Blank month | Nothing |
| Months before Mar 2025 | Import as history, or skip ❓ |
| Oct 2026 → Jul 2035 empty columns | Dropped; the app generates months as they come |
| Ins / Sign / Deposit or Access | `store_balances` opening values (sign convention ❓) |
| NOTES | First `note` on the store, dated the migration day; "STAX"/"WIRE" also sets the lease's payment channel |

**Check after import:** for every month from Mar 2025 to Aug 2026, the app's collected total and unpaid count must match the column sums above (e.g., Aug 2026: 392 collected, total $1,470,001.57, 47 unpaid).

---

## 7. Questions for the owner (Rents only)

1. **Positive** month values = the amount **collected** for that month?
2. **Negative** month values = that month's rent **not collected** (owed)? (493 of 648 equal exactly −rent, and most repeat the next month.)
3. When a positive value is **different from RENT**, is it because the rent changed over time, a partial payment, a catch-up, or a discount? Is the **rent history** written down anywhere?
4. **"9/9 pending"** = a debit started on 9/9 that hasn't settled yet? Started in Stax?
5. **Rec. Date** = the day of the month rent is debited?
6. **Ins / Sign / Deposit or Access:** positive = collected, negative = still owed? Are they one-time amounts?
7. **NOTES "STAX" / "WIRE":** is that the payment channel for the store? Are all stores **without** "WIRE" debited through Stax?
8. Is each month's ACH debit **started by staff in Stax**, or does Stax run it **automatically** (recurring schedule)?
9. **MALL / PLAZA / STREET / HEB** rows: real stores to include? Any ID for them?
10. **Store 2475** appears twice: two different spaces?
11. **68 stores** have a rent but nothing for Aug 2026: closed, terminated, or not open yet?
12. Import history **from Mar 2025**, or also the few entries from Sep 2024–Feb 2025?
13. Do cell **colors** on this tab mean anything (e.g., pending, late, closed)?
14. **Who edits** the Rents tab, and who should be allowed to change rent amounts in the new app?

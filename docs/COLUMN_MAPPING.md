# COLUMN_MAPPING.md — Rents tab → application tables

**Status:** DRAFT. Every row marked **PENDING Qn** depends on an owner answer listed in §6.
**Updated 2026-09-29:** Q1, Q2, Q3, Q5, Q6, Q13, Q21, Q22, Q24, Q26 and Q28 are closed by the owner in
writing (`docs/SCOPE.md` §9 Round 3). Cell notes are a new import source (§4).
**Source:** `docs/inputs/RENTS_AUDIT.md` (display values only; no formulas, colours or history).
**Target model:** `docs/SCOPE.md` §5.
**Rule:** no column is dropped silently. "Dropped" below means the value is still written to
`import_audit.raw_value` but produces no domain row.

Legend for "Kind": T = typed by staff. (The audit could not see formulas, so nothing is confirmed
calculated or looked up. Q15 asks.)

---

## 1. Row types (column A decides)

| Column A value | Rows | Becomes | Transformation | Open question |
|---|---|---|---|---|
| Numeric store number | 517 | `stores` (one) + `leases` (one, from D/F/G) | `external_label = A` verbatim; `store_number = int(A)` (non-unique) | **Settled (Q10):** 2475 is two different spaces (CONYERS, GA and Conyers (SALON)); import both as separate stores. The app's own primary key is the identity. |
| `MALL` / `Mall` | 10 | `stores` (one each) + `leases` | `external_label = A` verbatim; `store_number = NULL`; city distinguishes them | **Settled (Q9):** real, active, high-value stores ($6,200–$12,581). JENSEN MALL and ARBOR PLACE have "store closed" notes → lease `ended_on` unknown, `needs_review`. PEACHTREE MALL has no operator → lease with `operator_id = NULL`, `needs_review`. |
| `PLAZA`, `HEB 749`, `HEB 387` | 3 | `stores` + `leases` | `external_label = A` verbatim; for `HEB nnn`, `store_number = nnn` and a note `host = HEB` | **Settled (Q9):** real stores. PLAZA (VIERA OUTLET) is paused, see col G. (The audit's "STREET" row is not in the Round 1 list; import whatever column A holds under the same rule.) |
| `SHAHNAZ` (a person's name) | 1 | `stores` + `leases` | `external_label = "SHAHNAZ"`; Fort Pierce, FL; operator FOWAD; $3,900; day 15 | **Settled (Q9):** a real store whose label is a person's name. Import it, flag `needs_review` so the owner can relabel. |
| Blank | 1 | Dropped (audited) | — | — |

## 2. Fixed columns A–K

| Col | Header | Kind | Target | Transformation | Dropped? | Open question |
|---|---|---|---|---|---|---|
| A | Store | T | `stores.external_label` (verbatim, non-unique), `stores.store_number` (derived int when numeric) | See §1. Never used as a key. | No | Settled (Q9, Q10) |
| B | LOCATION, STATE | T | `stores.city` | Trim. If value matches `^(.*),\s*([A-Z]{2})$` (18 rows), split: city ← group 1, and compare group 2 with column C; mismatch → `needs_review` | No | — |
| C | STATE | T | `stores.state` | Upper-case, must be a valid US state/territory code; 1 blank → NULL + `needs_review` | No | — |
| D | Operator Name | T | `operators.name` (find-or-create by normalised name) + `leases.operator_id` | Normalise: trim, collapse spaces, case-insensitive match. 11 blanks → lease created with `operator_id = NULL`, `needs_review`. **Transfer syntax `A -> B`** (6 rows; Round 1c settled B is current: 1580, 2787 → aga; 4230, 6476, 5483 → asim; 2890 → GP): current lease → operator B; a second `leases` row for A with `ended_on = NULL`, `end_reason = "transfer (date unknown)"`, `needs_review`. Rows 4230, 6476, 5483 carry "LEASE TERMINATED IN JUNE 2026" → B's lease also `ended`, `ended_on = 2026-06-30`, `needs_review` | No | PENDING Q1x-transfer: is B always current? Is the transfer date known anywhere? (Q18) |
| E | Open Date | T | `stores.open_date` (date, nullable) + `stores.open_date_note` (text) + `stores.needs_open_date` | Any value that does not parse sets `needs_open_date = true`, which blocks insurance scheduling and nothing else; staff enter the date by hand (owner, 2026-09-29). Parse M/D/YYYY. 494 parse. Non-dates kept verbatim in `open_date_note`: `open` (14), `TBD` (3), `hold by WM` (1), blank (13). Malformed `2/12025` → try `2/1/2025`, flag `needs_review`. Two-line cell with `april` → date from line 1, note from line 2 | No | Q11 (relationship between open date and empty Aug 2026) |
| F | RENT | T | `lease_rents.amount_cents` with `effective_from = <migration month>`, `source = sheet_current` | Dollars → cents, must be > 0. 14 blank + 1 zero → no `lease_rents` row, lease `needs_review` | No | Q3 (rent history), Q14 (who may change rent) |
| G | Rec. Date | T | `leases.debit_days` (list) + `leases.collection_status` | Five value types (Round 1 counts): **day 1–25** (421) → `debit_days = [n]`, `active`. **Blank** (92; 21 WIRE, 13 no rent, 9 closed, 1 STAX, 12 collected in Aug 2026, ~50 unexplained) → `debit_days = []`, `collection_status = paused`, `review_reason` = `wire` / `dormant` / `closed` / `unknown` by the same signals; no debit is scheduled. **`Paused`/`paused`** (2) → `debit_days = []`, `collection_status = paused`. **`1 and 15`**, **`5 and 20`** (2: stores 3409 LEICESTER and 2629 Northbridge; 3409 shows $1,850 against $3,700 rent in Aug 2026, i.e. half) → `debit_days = [1,15]` / `[5,20]`, `active`, each charge = rent ÷ 2 (owner confirmed 2026-09-29, Q28); regex `^(\d{1,2})\s+and\s+(\d{1,2})$`. **`991`** (1) → `debit_days = []`, `needs_review` (typo). Any other text → `needs_review`. Days 26–31 do not occur; the app still applies a last-day-of-month rule. | No | Q5 closed by owner 2026-09-29: the day rent is debited. Q20 settled. Q28 closed: 50/50. |
| H | Ins | T | `store_balances` row, `kind = insurance_opening` (reference only) **and** `insurance_charges` rows built from the cell note | **Annual, not one-time (owner, 2026-09-29).** Number → cents, sign preserved: negative = current year unpaid. The cell note is parsed for patterns such as `2025 -> paid`, `2026 not paid`; each year named becomes an `insurance_charges` row on the current lease with that status and `legacy = true`. **Any value that is not exactly one known premium (595.00 or 695.00, either sign) is flagged `needs_review`; a multi-year sum is never split by arithmetic, only by what the note says.** No note or an unparseable note → `needs_review`, no charge rows. Text `no sign` → no row, note on store. | No | Q6 closed. Depends on the cell-note export (§4). |
| I | Sign | T | `store_balances` row, `kind = sign` | One-time. Number → cents, sign preserved: positive = paid, negative = still owed (owner, 2026-09-29). Text `Unknown`, `No sign fee` → no row, note on store | No | Q6 closed |
| J | Deposit or Access | T | `store_balances` row, `kind = deposit_or_access` | As I. Text `2/10 pending` → no row, note on store with `needs_review` | No | Q6 closed |
| K | NOTES | T | `notes` (one row per store, `author = "sheet import"`, `created_at = migration day`) **and** `leases.payment_channel` | Channel rule: contains `WIRE` (case-insensitive) → `wire`; contains `STAX` → `stax`; **neither (453 of 532 rows) → `payment_channel = unknown`, `collection_status = paused`, `needs_review`.** No lease with an unknown channel is ever enrolled in a Stax schedule. (Earlier draft defaulted these to `stax`; withdrawn 2026-09-27 because it would debit stores that have always paid by wire.) **Blocker for the first monthly run until Q7 is answered or a channel source is found.** Keywords `close`, `terminat` → store note flagged `needs_review` (possible ended lease). `discount`, `half`, `paid` → note only; no rule applied | No | PENDING Q7: channel meaning; are all non-WIRE stores Stax? |

## 3. Month columns L–EL (131 columns, Sep 2024 → Jul 2035)

One `rent_charges` row per lease per month where the cell is non-blank, `period` = column header month.

| Cell value | Target rows | Transformation | Open question |
|---|---|---|---|
| Positive number | `rent_charges` (`kind = rent`, `amount_cents = value`) + `payments` (`status = settled`, `method` from lease channel, `settled_on = last day of period` if no better date, `reference = "sheet import"`) | Dollars → cents. Charge amount is set to the collected amount because the sheet holds no separate "due" figure | Q1 closed by owner 2026-09-29. Q3 closed: causes are short payment, discount or credit, and cross-store balancing; which one applies to a cell is read from its cell note, otherwise `needs_review`. The grid is the only rent history. Import rule: a new value repeated for ≥ 2 consecutive months → `lease_rents` row (`source = sheet_inferred`, `effective_from` = first month), review-flagged (40 stores expected); a value below 60% of the current rent → `kind = rent` charge at full rent plus a settled `payments` row for the amount paid (partial, 34 stores expected); a value above rent that does not repeat → `kind = catch_up`, `needs_review`. Owner still owes: mid-month proration |
| Negative number | `rent_charges` (`kind = rent`, `amount_cents = abs(value)`, `workflow_status = unpaid`), **no** `payments` row | 493 of 648 equal −RENT. Where it does not, `needs_review` | Q2 closed by owner 2026-09-29 |
| `M/D pending` | `rent_charges` (`kind = rent`, `amount_cents` = current rent) + `payments` (`status = pending`, `initiated_on = M/D of that period's year`) | Regex `^(\d{1,2})/(\d{1,2})\s*pending$`. Year = period year. Only expected Aug–Sep 2026 | PENDING Q4 (meaning; started in Stax?) |
| `Closed` | `leases.ended_on = last day of period`, `end_reason = closed`; no charge | 3 cells | — |
| Any other text | No domain row; `import_audit.needs_review = true` | — | Report each to owner |
| Blank | Nothing | — | Q11 (Round 1c, now 63 stores): 18 with closed/terminated/sold NOTES → lease `ended`; 16 with Open Date ≥ Sep 2026 → `prospective`; 10 WIRE → active, channel wire; 3 with a Sep 2026 value → active; 16 with no signal → `paused`, `review_reason = unknown` |
| Columns Sep 2024–Feb 2025 | As above only if Q12 = yes; otherwise `import_audit` only | 10 cells across 5 stores: 2204 FOREST PARK $500 × 6; 2922 Tucson **−$3,800** (Nov 2024); 1444 JACKSONVILLE $3,740; 1083 JACKSONVILLE $3,740; 5300 GIBSONTON $3,831.25. Recommendation: import everything. | PENDING Q12, Q30 |
| Columns Oct 2026–Jul 2035 | Dropped (empty pre-created columns); app generates periods as they come | — | — |

**Post-import check (mandatory):** for every period Mar 2025–Aug 2026, sum of settled `payments`
and count of `unpaid` charges must equal the column sums in the audit (Aug 2026: 392 collected,
$1,470,001.57, 47 unpaid, 13 pending). Any mismatch fails the import.

## 4. Sheet concepts with no column (must still be captured)

| Concept | Where it lives in the sheet | Target | Note |
|---|---|---|---|
| Arrears | Implicit: consecutive negative months | Computed from `rent_charges` where `workflow_status = unpaid` | Never stored as a running balance |
| Rent change over time | Implicit: month value ≠ RENT | `lease_rents` | Inferred rows are `source = sheet_inferred` and reviewable |
| Operator transfer date | Not present | `leases.started_on` for the new operator = first month with a value after the transfer, `needs_review` | Q18 |
| Operator contact details | Not present on Rents | `operators.email`, `operators.phone` | Needed only for the receipt Stax sends after a payment; enrolment does not use them (owner, 2026-09-29). Q16 |
| Cell colours | Not exported | — | Q13 closed: green = positive, red = negative, nothing more |
| **Cell notes** (hover notes) | On individual cells; absent from every text or CSV export so far | `charge_notes` for month cells; `insurance_charges` status for column H; `notes` on the store for other columns | **Owner, 2026-09-29: they hold real data** (insurance year status, rent adjustments). Source is a spreadsheet-file export, where notes survive as comments. The file goes only in `docs/inputs/private/` and passes the Rule 1 pre-flight scan first. Each note keeps its cell reference in `import_audit`. A note that fails the Rule 1 validator is not imported and is reported. |
| Short payment carried forward | Implicit: a value below rent followed by a larger one | `charge_adjustments` (`reason = carry_forward`) only where the cell note says so; otherwise `needs_review` | Never inferred from amounts alone |
| Cross-store balancing | Implicit, explained in cell notes | `charge_adjustments` (`reason = cross_store_balance`) only where the note names the other store; otherwise `needs_review` | Both charges must belong to the same operator |
| Who edited what | Not exported | `notes.author` for new notes only | Q14 |

## 5. What is dropped

Only these produce no domain row: the blank row, the 120 empty future month columns, and free-text
values listed above as "note only". Every dropped cell is still recorded in `import_audit`.

## 6. Questions this mapping depends on

Numbered to match the Prompt 1 reply and `docs/SCOPE.md` §9. Q1–Q14 are the audit's own §7
questions, kept with their original numbers.

1. ~~Positive month values = amount collected for that month?~~ **Owner, in writing, 2026-09-29: confirmed.**
2. ~~Negative month values = that month's rent not collected (owed)?~~ **Owner, in writing, 2026-09-29: confirmed.**
3. ~~When a positive value differs from RENT: rent change, partial, catch-up or discount? Is rent history written anywhere?~~ **Owner, in writing, 2026-09-29: short payment, discount or credit, or cross-store balancing; rent history is written nowhere.**
4. "9/9 pending" = a debit started on 9/9, not yet settled? Started in Stax?
5. ~~Rec. Date = day of the month rent is debited?~~ **Owner, in writing, 2026-09-29: confirmed.**
6. ~~Ins / Sign / Deposit or Access: positive = collected, negative = still owed? One-time amounts?~~ **Owner, in writing, 2026-09-29: yes for the sign convention; Sign and Deposit/Access are one-time; Ins is annual.**
7. NOTES "STAX"/"WIRE" = payment channel? Are all stores without "WIRE" debited through Stax?
8. Is each month's ACH debit started by staff in Stax, or does Stax run it automatically today?
9. ~~MALL / PLAZA / STREET / HEB rows and the row with a person's name: real stores? IDs?~~ **Settled by data (Round 1).**
10. ~~Store 2475 twice: two spaces or a duplicate?~~ **Settled by data (Round 1): two spaces.**
11. 68 stores with a rent but nothing in Aug 2026: closed, terminated, not open yet?
12. Import history from Mar 2025 only, or also Sep 2024–Feb 2025?
13. ~~Do cell colours mean anything?~~ **Owner, in writing, 2026-09-29: only the sign of the value.**
14. Who edits the Rents tab today, and who may change rent amounts in the app?
15. Are any of columns A–K or the month cells formulas (the export showed values only)?
16. Where is the real operator contact list? The sheet has emails for about 12 of ~174 operators. **No longer a go-live blocker (owner, 2026-09-29): emails are needed only for receipts.**
17. ~~Stax invoice per store or per operator?~~ **Settled by data (Round 1): per store per debit.**
18. For "A -> B" operators, is B always current, and is the transfer date recorded anywhere?
19. Some operators exist in Stax already (39 rows say STAX). Can you export the Stax customer list (name, email, customer ID) so migration maps by ID instead of by nickname?
20. ~~Debit day 29–31 in short months?~~ **Settled by data (Round 1): no day above 25 exists; last-day rule implemented anyway.**
21. ~~Decision D1: confirm **D1-B** (Stax scheduled invoice with autopay) and the store-by-store parallel run described in `docs/SCOPE.md` §4?~~ **Owner, in writing, 2026-09-29: D1-B confirmed, with staff entering bank methods in the Stax dashboard and no hosted page for operators; store-by-store rollout.**
22. ~~Payment methods allowed on the Stax invoice: ACH only, or ACH and card?~~ **Owner, in writing, 2026-09-29: ACH only, no cards at all, no card exception; Zelle and wire recorded manually.**
23. Wire payments: should the app also mark the matching Stax invoice paid (so Stax shows it paid), or keep wires app-only?
24. ~~Partial payments: allow them on the Stax hosted page, or reject and require full rent?~~ **Owner, in writing, 2026-09-29: full amount required; the hosted-page part is moot.**
25. What is the account's per-transaction (High Ticket) limit for ACH? Rents go up to $12,581.
26. ~~CSV export: is the sheet's exact column layout (A–K + months) the format staff need for the parallel run?~~ **Owner, in writing, 2026-09-29: mirror the sheet during the parallel run, normalised export afterwards.**
27. Version history of the sheet: has it been searched for card or bank numbers yet?
28. ~~Twice-monthly leases (3409 LEICESTER "1 and 15", 2629 Northbridge "5 and 20"): is the monthly rent split 50/50 across the two debits? Data suggests yes.~~ **Owner, in writing, 2026-09-29: 50/50, two charge rows per period, grid shows the sum.**
30. Store 2204 FOREST PARK pays a flat $500 a month against a book median of $3,800: is that rent, or a different arrangement to model separately?
31. Of the 40 stores with a rent step change, were any mid-month (proration needed)?
32. Who among Ahmed, Zahir and Fatih gets which role (admin / billing / viewer)?
29. The 92 leases with a blank debit day: are they wire-channel, not yet started, or genuinely unknown? Until answered they are imported as paused.

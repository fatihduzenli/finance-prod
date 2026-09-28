# Answer sheet — Round 2 (owner), received blank 2026-09-27

Fill the `ANSWER:` lines and hand the file back. The answers go into `docs/SCOPE.md` §9 Round 2.

**Numbering crosswalk.** This sheet renumbers the session's questions. Session numbers are the
ones in `docs/COLUMN_MAPPING.md` §6 and `docs/SCOPE.md`.

| Sheet | Session | Topic |
|---|---|---|
| Q14 | Q16 | Operator contact list (blocker) |
| Q25 | Q25 | Stax ACH per-transaction limit (blocker, Stax) |
| Q1–Q8 | Q1–Q8 | Same |
| Q23 | Q28 | Twice-monthly split |
| Q24 | Q29 | Blank debit day |
| Q9 | Q11 | 68 stores blank in Aug 2026 |
| Q6 | Q6 | Same |
| Q15 | Q18 | "A -> B" transfers |
| Q16 | Q19 | Stax customer export |
| Q17 | Q21 | Decision D1 |
| Q18 | Q22 | ACH only or card |
| Q19 | Q23 | Wires marked in Stax |
| Q20 | Q24 | Partial payments |
| Q10 | Q12 | History before Mar 2025 (+ store 2204 flat $500) |
| Q11 | Q13 | Cell colours |
| Q13 | Q15 | Formulas |
| Q12 | Q14 | Who edits / who may change rent |
| Q21 | Q26 | CSV layout |
| Q22 | Q27 | Version history |
| G | — | Azure: hosting confirmed 2026-09-27 (ADR-0002); sandbox-or-live key still open |

Settled by data in Round 1 and not on this sheet: session Q9, Q10, Q17, Q20.

---
(Owner's sheet follows verbatim.)

# Business Questions — Answer Sheet (Rents tab)

**How to use:** fill in the `ANSWER:` line under each question. Short answers are fine — not full sentences.
**Why it matters:** the import script cannot be written until these are answered. You already know most of them; it should take 15–20 minutes.
**Then:** hand this file to Claude Code. The answers go into the Round 2 section of `docs/SCOPE.md`.

Most questions have a **[Data suggests]** line — the most likely answer based on the spreadsheet itself. If it is right, just write "yes".

## A. The two blockers (these hold up go-live)

### Q14. Where is the real operator contact list?
Only about **12 of 174 operators** have an email address anywhere in the sheet. Stax cannot send anything without an email, and autopay enrolment cannot happen either.
Is there a list somewhere else: phone contacts, signed leases, WhatsApp, accounting software, an older sheet?

**ANSWER:**

### Q25. Stax ACH per-transaction limit *(for Stax, not for you)*
To ask Stax: what is the ACH per-transaction limit on our account? **It must exceed $12,581** (our largest single rent).
Has this been asked, and has Stax replied?

**ANSWER:**

## B. What the month cells mean (the basis of the whole import)

### Q1. Is a positive number the amount collected for that month?
**[Data suggests]** Yes. 392 stores have a positive value in Aug 2026, and most match the store's rent exactly.

**ANSWER:**

### Q2. Is a negative number that month's rent not collected?
**[Data suggests]** Yes. Of 648 negatives, 493 are exactly minus the rent, and 540 are followed by another negative the next month — arrears building up.

**ANSWER:**

### Q3. When a positive value differs from RENT, what causes it?
A rent change, a partial payment, a catch-up payment, or a discount? **Can it be all of these, or does it always mean one thing?**
Also: **is the rent history recorded anywhere?** The sheet overwrites the old rent, so previous amounts are lost.
**[Data suggests]** More than one cause: 655 cells sit below the rent, 543 above it.

**ANSWER:**

### Q4. Does "9/9 pending" mean a debit started on 9 September that has not settled yet? Was it started in Stax?
**[Data suggests]** Yes — ACH takes several days to settle.

**ANSWER:**

### Q23. On the two twice-monthly leases, is the rent split 50/50?
Stores 3409 LEICESTER and 2629 Northbridge. On 3409, Aug 2026 shows $1,850 against a rent of $3,700 — exactly half.
**[Data suggests]** Yes, 50/50.

**ANSWER:**

## C. Stores and leases

### Q5. Is Rec. Date the day of the month the rent is debited?
**[Data suggests]** Yes.

**ANSWER:**

### Q24. What does a blank debit day mean, on the 92 leases that have one?
**[Data suggests]** Mostly "not on an automatic debit schedule": 21 have a WIRE note, 13 have no rent, 9 are marked closed, and only 12 collected anything in Aug 2026. The remaining ~50 are unexplained.
Does a blank mean not started yet, paid by wire, or simply not filled in?

**ANSWER:**

### Q9. The 68 stores with a rent but nothing in Aug 2026 — closed, terminated, or not open yet?

**ANSWER:**

### Q6. Ins / Sign / Deposit or Access columns: does positive mean collected and negative still owed? Are these one-time amounts?

**ANSWER:**

### Q15. For operators written as "A -> B", is B always the current one? Is the transfer date recorded anywhere?
There are 6 such rows.

**ANSWER:**

## D. Payment channel and how Stax is used today

### Q7. ⚠️ Do "STAX" and "WIRE" in NOTES mean the payment channel?
**Important:** **453 of 532 rows** contain neither word. The channel cannot be derived from NOTES for most stores.
Are all the stores with no note debited through Stax, or is the channel recorded somewhere else?
**The first monthly run cannot be built without this** — otherwise the app would try to debit stores that have always paid by wire.

**ANSWER:**

### Q8. Today, does staff start each month's debit by hand in Stax, or does Stax run it automatically on a recurring schedule?

**ANSWER:**

### Q16. Can you export the Stax customer list with their IDs?
Migration must match operators by Stax ID, not by name, or it will create duplicates.
Alternative: give Claude Code read access to the Stax account.

**ANSWER:**

## E. How the new system should work (decisions)

### Q17. Decision D1: Stax scheduled invoice with autopay, plus a store-by-store rollout — do you confirm?
Each operator saves a bank method once on Stax's hosted page, then it runs automatically. There is no single cutover day; operators move across one at a time.

**ANSWER:**

### Q18. On the Stax invoice: ACH only, or ACH and card?
**Note:** ACH is far cheaper (1%, capped at $10). A $3,800 rent costs about $10 by ACH versus roughly $76 by card.

**ANSWER:**

### Q19. Wire payments: should the app also mark the matching Stax invoice paid, or keep wires app-only?
**Recommendation:** mark it in Stax too, so there is a single ledger.

**ANSWER:**

### Q20. Should partial payments be allowed on the Stax hosted page?

**ANSWER:**

## F. Migration and working practice

### Q10. Import history from Mar 2025 only, or include Sep 2024 – Feb 2025?
**[Data suggests]** Import everything. Before Mar 2025 there are only 10 cells across 5 stores. One of them is store 2922 Tucson with **−$3,800** from Nov 2024 — arrears that would be lost if skipped.
Also: **store 2204 FOREST PARK pays a flat $500 a month** while others pay around $3,800. Is that rent, or a different arrangement?

**ANSWER:**

### Q11. Do cell colours carry meaning (paid / late / closed)?

**ANSWER:**

### Q13. Are any of the month cells formulas?
Click a few cells and look at the formula bar: is it just a number, or does it start with "="?

**ANSWER:**

### Q12. Who edits the Rents tab today? Who should be allowed to change rent amounts in the new app?

**ANSWER:**

### Q21. During the parallel run, is the sheet's existing column layout the CSV format staff need?

**ANSWER:**

### Q22. Has the sheet's version history been searched for card or bank numbers?
File → Version history → See version history. The *current* sheet has no card numbers anywhere, and the Rents tab has no bank or routing numbers. Older versions could still hold deleted data.

**ANSWER:**

## G. Also outstanding

### Azure / Key Vault
Hosting on Azure is confirmed (ADR-0002, 2026-09-27). Still needed: is the stored Stax key sandbox or live?

**ANSWER:**

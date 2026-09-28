# Evidence-based answers to the open questions — 2026-09-27

Received 2026-09-27. Data-derived findings and recommendations from a fresh CSV export of the
Rents tab. **Not owner statements.** Numbering follows the owner answer sheet
(`docs/inputs/ANSWER_SHEET_round2.md`), which differs from the session numbering; the crosswalk is
at the top of that file. Applied to `docs/SCOPE.md` §9 Round 1c and `docs/COLUMN_MAPPING.md`.

Note from the source: the live sheet changed between 2026-09-10 and 2026-09-27 (Q9 count 68 → 63).
The import pre-flight must run against the sheet as it is on migration day.

## Key findings (sheet numbering → session numbering)

- Q1→Q1 STRONG yes: positive = collected. ~80% of positives equal RENT to the cent.
- Q2→Q2 STRONG yes: negative = not collected; 493 of 648 equal −RENT; 540 followed by another negative.
- Q3→Q3 PARTIAL: all four causes occur. 40 stores show a permanent step change; 11 NOTES say "new rent from…"; 34 stores have a value below 60% of rent; a "$500 discount" note exists. **Rent history exists only in the grid.** Back-fill `lease_rents` from step changes at import, flagged for review. Owner still needed: mid-month proration.
- Q4→Q4 STRONG (in flight) / OWNER (platform): 48 "M/D pending" cells, Aug–Sep 2026, date always inside the column month.
- Q23→Q28 SETTLED: 50/50. 3409 LEICESTER $1,850 vs $3,700. 2629 Northbridge shows the combined total in the cell.
- Q5→Q5 STRONG yes: debit-schedule distribution; NOTES "auto payment on 15th", "7th auto payment".
- Q24→Q29 PARTIAL: of 92 blank debit days, 21 WIRE, 13 no rent, 9 closed, 1 STAX, 12 collected in Aug 2026, ~50 no signal. Import paused with `review_reason` wire / dormant / closed / unknown.
- Q9→Q11 PARTIAL, now 63: 18 closed/terminated/sold (NOTES), 16 not open yet (Open Date ≥ Sep 2026), 10 wire, 3 started Sep 2026, 16 no signal (28 of the 63 never had a month value).
- Q6→Q6 PARTIAL: one-time STRONG; sign convention **cannot be settled**. Termination Fees tab treats negative Sign/Insurance as credits, the opposite of "owed". Do not encode a guess; block balance reports until confirmed.
- Q15→Q18 SETTLED (B is current) / OWNER (date). Six rows: 1580 and 2787 Indianapolis ANWAR -> aga; 4230 Fort Wayne, 6476 Danville, 5483 Greenwood ANWAR -> asim ("LEASE TERMINATED IN JUNE 2026"); 2890 Macon SHAHNAZ -> GP (bought).
- Q7→Q7 PARTIAL: 48 STAX, 32 WIRE, 453 neither. `payment_channel = unknown` for the 453 is a **hard gate**: no autopay enrolment, no monthly run.
- Q8→Q8 PARTIAL: recurring auto-debits already run for a subset ("Auto payment stopped", "auto payment on 15th for 3 months"). Owner: what fraction, and what "pending" is operationally.
- Q16→Q19 OWNER: export Stax customers (dashboard export or `GET /customer`) before any migration code; match by Stax ID once, confirmed by owner, stored in `external_refs`.
- Q17→Q21 recommendation: confirm D1-B and store-by-store rollout.
- Q18→Q22 recommendation: ACH only; per-lease `allow_card` exception. Only 6 NOTES cells mention a card.
- Q19→Q23 recommendation: mark wires paid in Stax via "mark paid in cash or check"; owner confirms accounting treatment.
- Q20→Q24 recommendation: reject partials on the hosted page; agreed partials become adjusted charges in the app. Only 7 cells in Aug–Sep 2026 sit between 30% and 70% of rent, 2 of them the twice-monthly halves.
- Q10→Q12 recommendation: import everything. Pre-Mar-2025 = 10 cells: 2204 FOREST PARK $500 × 6 (Sep 2024–Feb 2025); 2922 Tucson −$3,800 (Nov 2024); 1444 JACKSONVILLE $3,740 (Dec 2024); 1083 JACKSONVILLE $3,740 (Jan 2025); 5300 GIBSONTON $3,831.25 (Jan 2025). 2204 flat $500 may not be rent (Q30).
- Q11→Q13 OWNER: colours invisible to export. Owner lists the scheme; the app's status field replaces it.
- Q13→Q15 OWNER, data argues against formulas (text mixed into the columns, no totals row). Owner checks five cells.
- Q12→Q14 OWNER: recommend roles admin (rent and debit-day changes, audited), billing (payments, notes), viewer. Names seen on other tabs: Ahmed, Zahir, Fatih.
- Q21→Q26 OWNER: mirror A–K + months during the parallel run only; normalised one-row-per-store-month afterwards.
- Q22→Q27 OWNER: version history not yet checked. Current sheet: Luhn scan of all 16 tabs found no card numbers; Rents has no bank or routing numbers.
- Q14→Q16 PARTIAL: emails for ~12 of 174 operators (~7%). Likely source: the Stax customer export (Q19), since operators on auto-debit already have an email in Stax. Remaining ~160 is a campaign that starts now.
- Q25→Q25 OPEN (Stax): must exceed $12,581. Also ask about a daily or monthly cap: day 1 carries 166 debits, roughly $600k.
- Azure: hosting already decided (ADR-0002). Remaining: label the stored key sandbox or live.

# QA — Step 12f-c, the owner's review of 12f-b on staging (four batches of fixes)

The plan is `docs/qa/PLAN-012f-c-staging-review-fixes.md`. One section per batch, appended as each
batch closes: scope, findings, departures, the gate (ruff, format, mypy strict, lock, makemigrations
on sqlite and PostgreSQL 16, the full sqlite suite, the CI split on PostgreSQL with the e2e suite
four times, the visual suite on PostgreSQL and sqlite with only the named baselines regenerated,
the secret scans, the provider fence), the shots (laptop 1280 and phone 390 of every changed page
from the Playwright image, under `docs/qa/12f-c/shots/<batch>/`) and the verdict.

## Batch A — Void and Refund on the payment page (branch `step-12f-c/batch-a`, plan D2–D6)

**The owner's question.** Refund and Void have existed since PLAN-009 for payments made through
the processor (ACH with a provider transaction), admin only, behind confirmation pages; the 12f-a
redesign kept them ("Refund" and "More ▾ › Void") and lost nothing. They are hidden on a payment
recorded outside the processor because the refusal rules require an ACH payment with a provider
reference. A void of a Zelle, wire, check or cash record never existed; it is new here.

**Scope.** Opus, one brief in two rounds (it stopped first to report that `Payment` had no
`voided_at` / `voided_by` and that the processor dialogs were already in the frame; Fable ruled:
three columns in migration 0057, the processor void stamps who and when from now on, the
installment sentence compares the plan's creation with `settled_on`, roles in the view and the
service, a derived attention kind). Built: `Payment.void_reason` (guarded, the payments trigger
recreated over it), `voided_at`, `voided_by`; `refunds.void_manual_payment` (billing and above; a
settled Zelle / wire / check / cash / other payment on a rent or one-off charge; a required reason
≤ 200 characters under the card-and-account guard; one transaction under the payment's and the
charge's locks; the charge recomputed through the existing functions so Paid becomes Partly paid
or Unpaid; the audit row `payment.voided_manual` with actor, reason, the charge before and after
and, when a marking of the charge's provider invoice by this payment succeeded, `provider_marking:
"to unmark"` with the invoice); `void_manual_facts` (the consequence sentence; the marked-invoice
sentence; the installment-plan sentence when a plan on the charge came after the payment); the
dialog `payments/void_manual.html` over the payment page (the States spec's frame, Reason,
"Keep the payment", the red "Void payment"); route `ledger:payments:void_manual` (billing, POST;
GET writes nothing; a foreign `?store=` is 404); "More ▾ › Void payment" in the header and the
MORE card; Voided (who, when) and Reason under DETAILS; the processor void's button red; the
derived attention kind `invoice_marked_paid_void` (HIGH: "Invoice {number} is marked paid in
{processor} for a voided payment — unmark it in the {processor} dashboard"), cleared by the
existing invoice refresh once the processor shows another status. Opus's
`tests/test_void_manual_payment.py` (41). Fable: `tests/test_plan012fc_a_adversarial.py` (14) and
the browser test `tests/e2e/test_void_manual_12fc_a.py` (the QA shots).

**Findings.** A-1 (MAJOR, resolved): a reason refused for payment data came back in the held
dialog's textarea (the class of 12f-b's B3-1); the held form keeps the error and drops the value.

**Departures from the owner's message, with reasons.** No reversal of a processor marking exists
(the processor's invoice statuses are system-managed and the app has no unmark call), so the void
tells the admin beforehand, flags the invoice in Needs review until the refresh shows it open, and
records "to unmark" in the audit row (plan D3; §9 asks). A credit applied from the lease, a credit
refund and an insurance payment are refused (their recompute paths are not the charge's). A split
payment's rows are voided one by one (each row is a payment of its own; the sibling rows and
charges are untouched). No strike-through exists for voided rows anywhere; the row reads Voided
as a processor void does. Promises closed by the payment are not reopened by its void.

**Gate.** ruff, `ruff format --check` (526 files), mypy strict (526 files), `uv lock --check`, `makemigrations --check` on sqlite: clean on 9e1f624 and again after A-1. sqlite: the full suite on 43506ed found the operator page's query budgets one over (the new Needs review rule's one query; re-pinned 52 → 53 and 63 → 64 by name) — 7,961 passed, 2 failed, then the file 37 passed on b066690. PostgreSQL 16 (a throwaway container, the CI split, on b066690): `makemigrations --check` no changes; the migration selection 36 passed in 1,553 s (0057's round trip included); the unit selection 8,621 passed, 3 skipped, 121 deselected in 1,695 s (the two-voids race ran here); the e2e suite 39 passed four times over (99, 100, 96 and 94 s). Visual: the `void-payment` baselines regenerated in the Playwright image against PostgreSQL (the red button), then every baseline compared: 66 passed on PostgreSQL and 66 on sqlite. Shots: `docs/qa/12f-c/shots/a/` — the More ▾ menu with "Void payment", the dialog, the voided payment page, laptop 1280 and phone 390, from the browser test run in the Playwright image against PostgreSQL. One note from the shots, recorded as A-2 (MINOR, deferred to Batch B by written decision): the voided payment's tiles still read "Remaining $1,500.00 refundable" (the refund tiles are the processor payments'); a voided payment should show nothing refundable. Secret scans clean (the pattern scan of the diff and the five new files, `scripts/check-no-secrets.sh`); no secret-store read; the provider fence clean; no new dependency or environment variable; migration 0057 round-tripped on sqlite by Opus and on PostgreSQL in the migration selection. Also found: the shared `_hold` helper drops only card numbers from a held form, not a routing number beside an account number — widened in Batch B.

**Verdict (A):** PASS — A-1 (MAJOR) resolved; A-2 (MINOR) deferred to Batch B; residual risk: the void never reaches the processor by design, so a marked-paid invoice stays paid there until a person unmarks it (the Needs review item carries it); the processor dialogs' GET paths were not re-shot beyond the regenerated baseline.

## Batch B — six copy and small-UI fixes (branch `step-12f-c/batch-b`, plan D7–D12)

**Scope.** Opus, one brief in two rounds (item h needed two files outside the first list; Fable
approved its approach), plus two items from Batch A's review. Built: (a) the greyed bank-debit
card shows the actionable reason first — "No bank account on file — send {operator} the
{processor} link to add one" — and "Bank debits are also off at {processor}." under it when both
apply; one line otherwise; (b) the Add payment badge reads "● Due · $2,100.00"; (c) phone numbers
read "(123) 456-7822" on screen through the `phone` filter (eleven digits with a leading 1 too;
another country code as "+CC …"; anything the filter does not understand as stored; the stored
value unchanged) on the operator page and side card, the Add payment summary and the operator
header line; (d) the Reference help reads "Never an account number." and the longer sentence
moved to the new Payments help topic, which also carries the void, partial-payment and
installment rules; (g) "Recorded by" and every "by {user}" cell show the staff member's name
through the `staff` filter (full name, else username, never the e-mail; the e-mail as the hover
title) on fourteen templates; (h) the payment page's Charge card shows "Partial payment — the
{processor} invoice stays open until the rest is paid." as an inline info notice while a balance
remains (the review message's second-invoice fallback never shown for it; a charge completed by a
later partial payment keeps the strong line, since its invoice is still not marked); from Batch
A's review: the shared held-form helper drops any value the payment-data guard refuses (a routing
number beside an account number, not only a card number) across every held form, and a voided
payment has nothing refundable ("$0.00 · nothing refundable"). Opus's `tests/test_plan012fc_b.py`
(58). Fable: `tests/test_plan012fc_b_adversarial.py` (18) and the browser test
`tests/e2e/test_copy_fixes_12fc_b.py` (the QA shots).

**Findings.** None beyond the two carried in from Batch A's review (the held-form guard, the voided payment's tile), both resolved here. Two pins moved by design and were re-pinned by Fable: `test_check_method` (Add payment's Reference help is the one line) and `test_plan012a_adversarial` (the operator page formats the SMS phone).

**Departures from the owner's message, with reasons.** The "send {operator} the {processor} link"
sentence is built as written though the app sends no link and the 2026-09-29 decision keeps
operators off processor pages (plan §9 asks). Three "by {user}" cells still show usernames (the
store cost history, the collections log's "Sent by", the audit and History "User" columns): their
services pass usernames as strings and were outside the batch; recorded for Step 13. The partial
notice shows only while a balance remains (see h). The CSV exports keep stored phone values.

**Gate.** ruff, `ruff format --check` (531 files), mypy strict (531 files), `uv lock --check`, `makemigrations --check` (no migration): clean on 1ec012c. sqlite: the full suite 8,039 passed, 3 failed (the two re-pins above, one of them twice) in 1,677 s; the two files 30 passed on 63b3ed5. PostgreSQL 16 (a throwaway container, the CI split): `makemigrations --check` no changes; the migration selection 36 passed in 1,528 s; the unit selection's first run 8,699 passed and 1 failed — `test_plan012f_d_pages::test_the_pages_with_a_parent_carry_its_breadcrumb`, whose traceback the pipeline script did not keep (it kept summary lines only; it keeps the raw output now); the test passed alone three times and in its file three times in random order on PostgreSQL, and the whole unit selection run again passed 8,700 (1,133 s) — recorded as unexplained rather than fixed; the e2e suite's first four passes each had the one browser pin of the old Reference help (re-pinned ecf46cd), then 41 passed four times over (77, 75, 76 and 74 s). Visual: six baselines regenerated in the Playwright image against PostgreSQL (`add-payment` ×2: the badge and the two-line reason; `help` and `help-topic` ×2: the Payments topic in the list), then every baseline compared: 66 passed on PostgreSQL and 66 on sqlite. Shots: `docs/qa/12f-c/shots/b/` — Add payment with the two reasons and the badge, the payment page with the partial notice and the staff name, the operator page with the formatted phone, the Payments help topic, laptop 1280 and phone 390, from the browser test run in the Playwright image against PostgreSQL. One note from the shots, not fixed: a settled Zelle payment's third tile reads "Remaining $600.00 refundable" though refunds apply to processor payments only (Batch A's A-2 covered the voided case); recorded for the owner. Secret scans clean (the pattern scan of the diff and the six new files, `scripts/check-no-secrets.sh`); no secret-store read; the provider fence clean; no new dependency, URL, model or environment variable.

**Verdict (B):** PASS — no new findings; the two items carried from Batch A resolved; residual risk: the one unexplained PostgreSQL failure of the breadcrumb pages test in the first unit run (passes everywhere since; the pipeline now keeps tracebacks), the "refundable" tile wording on non-processor payments, and the three username cells left for Step 13.

## Batch C — Split into installments to the spec (branch `step-12f-c/batch-c`, plan D13)

**Scope.** Opus, one brief in three rounds. Built: the page in the Add payment family — the full
breadcrumb, the title with the charge's status badge, the muted line "{charge} · #no Name ·
operator · {balance} left to pay"; the Plan card (the − / + stepper around a number input 2–12,
the first installment date echoed as "Oct 26, 2026", the Monthly / Weekly / Yearly segmented
control, "Collected by"); the live preview from the read-only JSON route `ledger:plans:split_preview`
(billing; every parameter validated; refusals as `{"ok": false, "message"}`; the server renders
the same table from the query string with scripts off), the rounding cent marked on the last row,
the editable dates and amounts kept under the engine's rules; the sticky Summary (Balance to
split, "n × share", Last payment, the returned-payment fee line, "Create n installments", Cancel);
phone: one column, the Summary above the primary. The three-line help moved to the Payments
topic; `SPLIT_HELP` and `SPLIT_CADENCES` removed as dead code. DESIGN-SPEC §4 gains "Split into
installments" (Fable, from the built page). Opus's `tests/test_split_page.py` (59). Fable:
`tests/test_plan012fc_c_adversarial.py` (28) and the browser test `tests/e2e/test_split_page_12fc_c.py`
(the QA shots).

**Findings.** C-1 (MAJOR, the owner's words, resolved): the control offered the old split
cadences (Weekly / Every two weeks / Monthly); it offers Monthly / Weekly / Yearly as asked —
"Every two weeks" is no longer offered on this form (existing plans untouched; the reschedule
form unchanged). C-2 (MINOR, resolved): the page answered 200 for another store's `?store=`; it
answers 404 as Add payment does, and the preview route shares the rule.

**Departures from the owner's message, with reasons.** The breadcrumb keeps the family's store
crumb ("#909"); "#no Name" is in the muted line. The first values (3, today, monthly) are previewed
on open so the table and Summary are filled without the script. The large-debit tick is always in
the markup for autopay collection and shown by the script only above the threshold. The
returned-payment fee line is Opus's wording from the Autopay help. The route digest is 275 (274
after Batch A plus this route).

**Gate.** ruff, `ruff format --check` (535 files), mypy strict (535 files), `uv lock --check`, `makemigrations --check` (no migration): clean on 80ecffb. sqlite: the full suite 8,126 passed, 3 failed — two settlement pins moved by C-1 (re-pinned by Fable: Split offers Monthly / Weekly / Yearly and refuses every two weeks) and one flaky assertion in `test_operator_workspace` that matched the digits "9009" inside a random hex id (hardened to a number boundary) — then the two files 96 passed on ad87b2a. PostgreSQL 16 (a throwaway container, the CI split, on ad87b2a): `makemigrations --check` no changes; the migration selection 36 passed in 975 s; the unit selection 8,787 passed, 3 skipped, 125 deselected in 1,192 s; the e2e suite 43 passed four times over (81, 78, 77 and 78 s). Visual: no baseline regenerated (the split page is not a visual-suite page and no shot page changed); every baseline compared: 66 passed on PostgreSQL and 66 on sqlite. Shots: `docs/qa/12f-c/shots/c/` — the page as it opens (3 × $333.33, the rounding cent on the last row) and after one press of + (4 × $250.00), laptop 1280 and phone 390, from the browser test run in the Playwright image against PostgreSQL. Secret scans clean (the pattern scan of the diff and the four new files, `scripts/check-no-secrets.sh`); no secret-store read; the provider fence clean; no new dependency, model, migration or environment variable; one new read-only URL.

**Verdict (C):** PASS — C-1 (MAJOR) and C-2 (MINOR) resolved, none deferred; residual risk: the script's failed-fetch path (the Preview button returning) was exercised only by Opus's throwaway probe, not by a committed test; the page's query count is not pinned.

## Batch D — the USPS infra slice (branch `step-12f-c/batch-d`, plan D14)

**Scope.** Opus, one brief. Built: `tests/test_usps_spec.py` (18 cases) reads the committed OpenAPI file `docs/integrations/usps/addresses-v3r2_0.yaml` (version 3.3.1) with a small strict reader (PyYAML is not a dependency) and proves the client's path and method, its four query parameters against the operation's declared parameters, patterns and lengths and the "city and state, or ZIPCode" rule, the 200 answer's fields the client reads, the 404 and 401, and the token URL and scope — no code mismatch; the client's docstring cited an older file version and said `state` was required, corrected. Bicep: `containerAppWeb.bicep` gains the optional parameters `uspsClientIdSecretName` and `uspsClientSecretSecretName` on the Sentry pattern (the Key Vault references `usps-client-id` and `usps-client-secret`, env `USPS_CLIENT_ID` / `USPS_CLIENT_SECRET` from `secretRef`, `USPS_API_BASE_URL` = `https://apis.usps.com` always set); `main.bicep` passes them to the web module alone; both parameter files carry the two lines commented out with the uncomment note; `infra/README.md`'s procedure. `az bicep build` passes. Fable: the two web/worker parity pins re-pinned (the USPS settings and secrets are the web app's alone).

**Findings.** None. Two parity pins moved by design (re-pinned by Fable): `test_infra_mail` and `test_jobs` assert the worker carries every web secret and setting except the documents' — the USPS ones join that exception.

**Departures and notes.** The OpenAPI file is `docs/integrations/usps/addresses-v3r2_0.yaml` (the
owner's message said `addresses-v3.yaml`); the Key Vault secret names the parameters expect are
`usps-client-id` and `usps-client-secret` in `finance-sandbox-kv` (and `finance-kv` for
production), shipped commented out until the secrets exist, because a Key Vault reference to a
missing secret stops the revision from starting. The Bicep deploys only on an owner-authorised
infra apply (Section 6); this PR carries the files. The form keeps saving with "Address not
verified" until then.

**Gate.** ruff, `ruff format --check` (536 files), mypy strict (536 files), `uv lock --check`, `makemigrations --check` (no migration), `az bicep build` (no cloud call): clean on 44a3584; the USPS and infra test files 198 passed. sqlite: the full suite 8,147 passed, 697 skipped, 122 deselected in 1,003 s, no failure. PostgreSQL 16 (a throwaway container, the CI split, on 44a3584): `makemigrations --check` no changes; the migration selection 36 passed in 954 s; the unit selection 8,805 passed, 3 skipped, 125 deselected in 1,151 s; the e2e suite 43 passed four times over (80, 78, 80 and 78 s). Visual: no baseline regenerated (no page changed); every baseline compared: 66 passed on PostgreSQL and 66 on sqlite. Shots: none (no page changed). Secret scans clean (the pattern scan of the diff and the two new files, `scripts/check-no-secrets.sh`); no secret-store read; no vault read; no `az deployment` of any kind; no new dependency; no secret value anywhere (names only).

**Verdict (D):** PASS — no findings; residual risk: the wired branch of the Bicep (the two Key Vault references resolving at deploy time) is untested until the owner stores the secrets, uncomments the parameters and authorises the infra apply; the live USPS API was not called (TODO-VERIFY-065 stands).

## Slice E — the owner's checkpoint decisions (branch `step-12f-c/batch-e`, plan §11)

**Scope.** Opus, one brief. E1: the Needs review item for a voided payment whose processor invoice
stays marked paid reads "Unmark invoice {number} in {processor} by hand; this payment was voided".
E2: the greyed bank-debit card's first line is "No bank account on file for {operator}." with an
"Operator page" link to the operator's page; "Bank debits are also off at {processor}." under it
when the flag is off as well; the dashboard instruction moved to the Payments help topic. E3: a
payment recorded outside the processor shows two tiles, Amount and "Applied to {charge}", and the
muted line "Paid outside {processor}: void it to undo, it cannot be refunded here."; processor
payments keep Amount / Refunded / Remaining. Opus's `tests/test_plan012fc_e.py` (21) and the re-pins by name in `test_plan012fc_b.py`, `test_void_manual_payment.py`, `test_add_payment_page.py`, `test_bank_debit.py`, `test_rent_debit.py`. Fable: the Batch A and B adversarial and browser files re-run; the Batch B browser test takes slice E's shots.

**Findings.** E-1 (MINOR, resolved): a voided manual payment's "Applied to" tile still showed the full amount; it reads $0.00 with "{charge} · voided". Departures, with reasons: How to collect (the invoice builder) keeps its "add one in the {processor} dashboard" sentence — the owner's decision named the Add payment card, and the builder's shared constant would print a literal placeholder; the tile reads "Applied to" with the charge as its sub-line (the KPI pattern); manual payments on insurance or adjustment charges keep the three tiles, since "void it to undo" would be false for them. Also fixed: Fable's own card-number assertion matched the digits "4111" inside a random hex id once (the class of the "9009" flake), hardened.

**Gate.** ruff, `ruff format --check` (537 files), mypy strict (537 files), `uv lock --check`, `makemigrations --check` (no migration): clean on 68e64d4. sqlite: the full suite 8,168 passed, 697 skipped, 122 deselected in 1,006 s on d24baee. PostgreSQL 16 (a throwaway container, the CI split, on d24baee): `makemigrations --check` no changes; the migration selection 36 passed in 1,582 s; the unit selection 8,826 passed, 3 skipped, 125 deselected in 1,756 s; the e2e suite 43 passed four times over (107, 105, 103 and 104 s). Visual: the `add-payment` baselines regenerated in the Playwright image against PostgreSQL (the sentence and the link; `payment-detail` regenerated and unchanged, its payment being ACH), then every baseline compared: 66 passed on PostgreSQL and 66 on sqlite. Shots: `docs/qa/12f-c/shots/e/` — Add payment with the new sentence, the Operator page link and the second line; the Zelle payment's page with Amount and "Applied to" and the "Paid outside" line, laptop 1280 and phone 390, from the Batch B browser test run in the Playwright image against PostgreSQL. One observation, not changed: the manual payment's page still shows the empty Refunds card ("No refunds."); the owner's decision replaced the tiles only. Secret scans clean (the pattern scan of the diff and the one new file, `scripts/check-no-secrets.sh`); no secret-store read; no new dependency, URL, model or environment variable.

**Verdict (E):** PASS — E-1 (MINOR) resolved; residual risk: none beyond the How to collect sentence left as is (noted for the owner).


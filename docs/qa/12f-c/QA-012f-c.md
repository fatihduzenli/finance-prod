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


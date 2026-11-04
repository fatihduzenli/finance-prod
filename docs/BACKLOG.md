# BACKLOG.md — Phase 7, after Step 17

**Date:** 2026-10-07 (Prompt 12). Read `docs/CLAUDE.md` and `docs/SCOPE.md` §6 first. This file
captures the "later" list so it stops competing with migration (Step 13) and cutover. Spec facts
below were read from `docs.staxpayments.com` on 2026-10-07 (the `.md` pages named in each row).

**Confirmed in writing: nothing in §1 is started until after Step 17.** Items the owner's
amendments moved *into* Step 12 (disputes read-only list, contact-log changes, the invoice
builder) are in `docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md`, not here.

## 1. Items

| # | Item | What it does | Stax endpoints (verified) | Effort | Depends on | Risk it introduces |
|---|---|---|---|---|---|---|
| 1 | **Reminders by Stax e-mail or SMS** (Step 12 item 1) | Re-send an overdue invoice through Stax instead of, or as well as, our own ladder (built in 11b through the company mailbox). SMS needs a phone in international format and the operator's consent. | `PUT /invoice/{id}/send/email` (send-an-invoice-via-email), `PUT /invoice/{id}/send/sms` (send-an-invoice-via-sms: query `message`, `phone` as `+1…`; only finalized invoices; sets `SENT`/`sent_at`; may be resent). | S (2–3 days): a third delivery channel in the ladder, a consent field, a per-operator channel choice. | 11b ladder; owner decision on SMS consent wording; TCPA review for SMS. | **An automated loop that calls Stax**: every resend is an API call under the 100/min and 10-failures/min rules; a bug can lock the IP for an hour. Must run through the worker and the daily tick only, with a per-day cap and the breaker. |
| 2 | **Scheduled (recurring) invoices** (Step 12 item 2) | Already built: D1-B uses `POST /invoice/schedule/` for rent and insurance; one-occurrence schedules for autopay_due and re-debits. | `POST /invoice/schedule/`, `GET /invoice/schedule`, `PUT/DELETE /invoice/schedule/{id}` (scheduled-invoices). | Done (owner: skip). | — | Open: TODO-VERIFY-012/019 behaviour on returned debits and day 29–31, verified once ACH works. |
| 3 | **Catalog synced to Stax** (Step 12 item 3) | Mirror our `fee_types` as Stax catalog items so dashboard-made invoices pick the same lines. | `GET/POST /item`, `PUT/DELETE /item/{id}`, `GET /item/code` (catalog, create-catalog-item, update-an-item, retrieve-all-item-codes). | S. | Owner: **skip** (the fee catalogue stays local; dashboard-made invoices are linked, not generated from a catalog). | Two sources of truth for prices; a sync loop that writes to Stax. |
| 4 | **Disputes with evidence upload** (Step 12 item 4) | List the merchant's disputes with status, reason, amount, respond-by; upload evidence files; link each dispute to our payment and operator. The read-only list with evidence upload is **built in Step 12** (owner amendment); the backlog part is the full workflow: accept a dispute, track outcomes, reminders before `respond_by`. | `GET /underwriting/disputes` (list-disputes: filters `id`, `transaction_id`, `status` in INQUIRY, OPEN, PENDING, ACCEPTED, PREARBITRATION, WON, LOST, EVIDENCE_UPLOADED, UPLOAD_FAILED; `reason` in TECHNICAL, CLERICAL, QUALITY, FRAUD, INQUIRY; `sort` default `respond_by`; `startDate`, `endDate`, `page`; object: `id`, `transaction_id`, `merchant_id`, `reason`, `amount`, `status`, `respond_by`, `created_at`, `updated_at`), `POST /file/dispute` (upload-evidence-for-dispute: multipart `dispute_id`, `name`, `file` < 10 MB, .jpeg/.pdf/.png/.tiff; 400 after `respond_by`), `DELETE` evidence (delete-evidence-for-dispute), submit for review (submit-dispute-evidence-files-for-review; page exists, body to be read when built), webhooks `create_dispute`/`update_dispute` (handled today: the payment is flagged). | M (1 week) for the workflow beyond the Step 12 list. | Step 12's list; documents path (10c) for the files; the dispute webhook. | Evidence files are documents: Rule 1 applies (a bank statement as evidence must not carry account numbers into the app; the 10c guard runs on PDFs; images by policy). Deadlines are money: a missed `respond_by` loses the dispute. |
| 5 | **Surcharging** (Step 12 item 5) | Pass a card fee to the payer. **Not applicable while the app is ACH-only and card-free (SCOPE §2, Rule 1)**; recorded for a future card decision. Stax's compliant surcharging product is **CardX**; the Stax reference exposes only a reporting endpoint. Card-brand rules (Visa/Mastercard notification, caps around 3 %, no surcharge on debit) and state laws (a few states restrict or condition surcharges) apply. **Requires legal advice before any build.** There is a reported case of Stax's underlying processor not supporting compliant surcharging; verify with Stax in writing. | `GET /query/statement/v3/surcharges` (surcharging-1: `startDate`, `endDate`; rows `card_type`, `sales_amount`, `surcharges_amount`, `surcharges_count` with a `summary`); surcharge information on a transaction (review-a-transactions-surcharge-information). No creation endpoint in the merchant API; CardX is a separate product. | L and legal. | A decision to accept cards at all (today: never); legal review; CardX contract. | Legal exposure; customer disputes; the invoice total model must keep fees and surcharges separate (see §2). |
| 6 | **Second provider: Stax Bill** (Step 12 item 6) | Subscription billing behind `PaymentProvider`. Stax Bill is a **separate product with its own API and developer portal** (not the merchant API this app uses); it would be a new adapter package. | Not in the Stax merchant reference; Stax Bill's own portal (not read for this file). | L (3–4 weeks) for the adapter and the mapping; the interface is in `docs/PROVIDER.md` §2. | `docs/PROVIDER.md`; an owner decision that subscription billing is wanted. | A second ledger of truth for schedules; two keys to protect; two webhook formats. |
| 7 | **Provider portability** (Step 12 item 7) | Done in Step 12 as documents and a test: `docs/PROVIDER.md` and `backend/tests/test_provider_portability_adversarial.py`. No code. | — | Done. | — | — |

## 2. "Cheaper now than later": schema recommendations for the owner's decision

Each is a decision, not a change. **Both accepted by the owner on 2026-10-07 (ADR-0011)
and built in PLAN-012a the same day; the columns exist, empty, before Step 13.**

| # | Recommendation | Why now is cheaper | Cost now | Cost later |
|---|---|---|---|---|
| S1 | **Line-item kind.** Give `invoice_line_items` a `kind` column (`charge`, `fee`, `surcharge`, `tax`, `discount`) and a nullable `one_off_charge` reference, and keep `invoices.total = subtotal + tax + fees + surcharge` as separate stored columns (`fees` and `surcharge` default 0). | The Step 12 invoice builder is the first code that writes several line items per invoice; adding the discriminator while that table is being populated avoids a backfill over every invoice later. Item 5 asks for fees and surcharges to be "a separate concept so they can be added later without a schema change". | One migration with defaults; the builder writes `charge` lines. | A backfill over thousands of lines with guessed kinds, and a change to every report that sums `total`. |
| S2 | **Operator contact consent.** `operators.sms_phone` (E.164), `sms_consent_at`, `email_consent_at` (nullable timestamps) now. | Item 1's SMS channel and any future reminder channel need a recorded consent; the Step 13 import is the one moment every operator row is written with sheet data, so the columns can be filled then from the sheet's phone column where present. | Three nullable columns; the import maps the phone. | A second pass over 450 operators after cutover, by hand. |

Not recommended now: a `disputes` table (Step 12 builds it, read-only, as the amendment asks);
a `provider` discriminator beyond `external_refs.provider` (already there); any card or surcharge
column on `payments` (the app is card-free).

## 3. Why this list stays frozen

Migration (Step 13) and cutover are where money goes missing. Every item above either calls
Stax in a loop (1, 3), handles money state (4, 5) or adds a second source of truth (6). The
feature set is frozen until after Step 17 (Checkpoint 12); the owner tells the staff in writing.

## 4. Test-suite cost (added 2026-10-07)

The seeded migration round trips (`test_migrations_roundtrip.py`: 0031, 0032, 0033 and the
fresh-database trip) and the rename-migration tests in `test_one_off_charges.py` take about two
minutes on sqlite and several on the CI PostgreSQL service; the CI unit step limit was raised to
25 minutes on 2026-10-07 after a 15-minute timeout. Before Step 13 adds its own migration tests:
run the round trips as their own CI step with their own limit, or build the migrated database
once per run and reuse it; consider `--reuse-db` for the non-migration tests. Not a feature; may
be done inside the freeze.

**Done 2026-10-14 (PLAN-012b-a part 1).** The 12b push ran the unit step 22:11 of 25. The
seeded migration tests (`test_migrations_roundtrip.py`, `test_report_indexes.py`, the two rename
tests of `test_one_off_charges.py`, the concurrent-migrate test of
`test_staging_defects_adversarial.py`: 21 tests, about 8 minutes on PostgreSQL) now run as their
own CI step, "Migration tests", limit 15; the unit step runs the complement (limit 20, measured
about 14 minutes locally). Still open: reuse of the migrated database and `--reuse-db`, if the
unit step grows past 15 again.

## 3. Follow-ups queued right after the Step 12e push (owner, 2026-10-22), before Step 13

| # | Item | What it does | Notes |
|---|---|---|---|
| F1 | **"Check" as its own manual payment method** | A `PaymentMethod` value `check` with the label "Check", offered beside Zelle and wire wherever a manual payment is recorded (Add payment, the builder's Record payment now, the insurance and one-off payment forms, the statement and ledger labels); its reference field reads "Check number". | A new stored value: a choices migration, the frozen-value list in `tests/test_plan012c_design_d_adversarial.py` updated by Fable, `rent_run.MANUAL_METHODS` extended, the refund method already has `check`. Decided with Step 12e decision 3 (until then a check is "other" with its reference). |
| F2 | **The invoice builder's lines table in a scroll region** | At 1,280 px the lines table makes the page 2,031 px wide; it goes into the `.table-scroll` region the other wide tables use, so the page stays within the viewport and the visual baseline of the builder page is regenerated. | Pre-existing since PLAN-012d; recorded in QA-012e-2 Batch D as a deferred MINOR. |

## 3. After Step 14 (owner, 2026-11-02): small items, none blocking

Each is a small change with the usual gate. None touches money; none needs a decision beyond the
owner's "do it". Recorded here so they stop competing with the live checks of Step 14.

| # | Item | What is wrong today | What to do | Effort | Notes |
|---|---|---|---|---|---|
| B1 | **CI unit shard A near its headroom** | Shard A (`test_a…`–`test_i…`) ran 14:42 and 14:10 on PRs #39 and #38 against the owner's 15-minute bar (step limit 25). | Re-measure the three shards' per-file times on PostgreSQL (`pytest --durations=0`) and move files between A and C (keep C the "rest" glob); keep `test_ci_shards.py`'s pin. | XS | Measure, don't guess: the count-based split timed out once (QA-014). Measured 2026-11-04 (Opus, per-file timings): sqlite A 8.8 / B 8.9 / C 9.6 min; PostgreSQL A 14.3 / B 12.5 / C 15.4 of 42.2 in all (CI jobs 10–15 min each). No three-way file split brings A under the bar — it is at the three-way optimum; `B = test_[np]*` only moves 1 min from C. Under 15 min with headroom needs a fourth shard (a CI matrix change) or faster tests. Owner's decision. |
| B2 | **Log contact: the "Kind" label sits tight under the about line** | In the dialog the "Kind" label has no gap after "Northwind Stores, store 0101 MAPLE AVENUE". | A spacing token between the about line and the first field; both themes and widths; the dialog's baselines move. | XS | Found in the combined gate's shots (QA-014). |
| B3 | **Log contact: Cancel/Save below the fold with an open promise** | With an open promise the dialog grows (Previous promise, What happened) and its buttons sit below a laptop's fold; the dialog scrolls inside. | Pin the Cancel/Save row to the dialog's bottom (sticky footer) or collapse the promise block until "Promise to pay" is chosen. | S | Same shots. |
| B4 | **A consent time typed in the repeated DST hour saves an hour early** | A `datetime-local` value in 01:00–02:00 New York on the fall-back day is ambiguous; Django reads it as the first occurrence (standard time), one hour before the second. | Resolve ambiguous local times with `fold=1` when the typed time is after the clock went back, or refuse the ambiguous hour with one sentence; a test on the 2026-11-01 and 2027-11-07 fall-back days. | XS | Found by gate I-b run 1 on 2026-11-01 (QA-014). Affects two fields; once a year. |
| B5 | **The daily job runs at 08:00 New York in winter** | `infra/modules/containerAppJobDaily.bicep` cron `0 13 * * *` is UTC: 09:00 New York in daylight time, 08:00 in standard time; messages queued before 09:00 wait for the send window on the worker. | Either two crons (13:00 UTC in summer, 14:00 UTC in winter) switched by the Bicep parameter at the clock changes, or a single 14:00 UTC cron (10:00 in summer), or keep as is and accept the one-hour hold. Owner's choice; the change is a Section 6 deployment. | XS | CHECKPOINT-14-2 §5 names it. |
| B6 | **Azure deploy credential: switch to the ID-format subject, remove the classic one** | On 2026-11-02 the staging deploy failed (AADSTS700213): the app registration `finance-github-deploy` had only an ID-format federated subject (`repo:<owner>@<id>/<repo>@<id>:environment:staging`) while GitHub presented the classic one; the owner's Option A added a classic-subject credential (`github-staging-classic`) beside it, so both are trusted now. | When GitHub's repository OIDC subject is switched to the ID format (repository settings → Actions → OIDC subject claim), the deploy presents the ID subject; then remove `github-staging-classic` so only the ID-format credential remains. Verify with one deploy before removing. | XS | Two Section 6 actions (the GitHub setting, the credential removal); each its own yes. |
| B7 | **A released message keeps its queue day's "N days overdue"** | A message held for the settings and released days later goes with the headline the queue wrote ("5 days overdue") while the charges list is rebuilt from the day of the release, so the headline and the list can disagree by the days held. | Rebuild the headline with the list at the release (`messaging.plan_release`), as the send rebuilds a combined message's charges. | XS | Found in the settings-guard review (2026-11-03); affects only messages released by "Send all held for settings" or "Send now" after a hold. |
| B8 | **A monthly provider invoice for the "E-mail invoice" channel** | The New lease page's "E-mail invoice" card records the channel only: the month's rent charge comes from the rent run and reminder rule B's due notice (with the pay link) is the monthly e-mail — no invoice is created at the processor each month. The card now says so ("We e-mail a reminder with a pay link before each due date"). | A decision for the owner and the manager: build a monthly provider invoice for this channel (created and sent by the daily run, reconciled like a sent invoice), or keep rule B's notice as the channel's e-mail. | S–M | Decision first; no code until it is taken. |

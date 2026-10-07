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

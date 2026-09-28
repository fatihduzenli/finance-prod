# STAX_ANSWERS.md — Stax onboarding answers

**Status:** INTERIM. Stax has **not yet replied in writing**. The answers below are the public-doc
answers compiled on 2026-09-10 (`docs/inputs/STAX_ANSWERS_public_2026-09-10.md`), reproduced
**verbatim** in §1. When Stax's written reply arrives, paste it under the matching
"Stax written reply (verbatim)" line and change the status. Do not paraphrase Stax.

§2 flags which answers change the design **for the Rents scope**. §3 records one correction found
on 2026-09-16.

---

## 1. Answers, verbatim (public documentation, 2026-09-10)

## Q1. Card-not-present per-transaction fee

**Status:** ANSWERED (public). Confirm it matches the signed merchant agreement.

**Public answer:** The pricing page lists a flat fee of $0.15 per transaction for keyed-in or online payments (card not present), on top of interchange with 0% markup. Card present is $0.08. ACH is 1% per transaction, capped at $10. Subscription is $99/month up to $150,000/year processed, $139/month for $150,000–$250,000, and $199+/month above that. Credit/debit surcharging is also listed: merchant debit surcharge pricing of 1.25% + $0.25.

**Also on the pricing page:** next-business-day funding, data exports (transactions, customers), hosted payment pages, recurring billing, API key integration, and no cancellation fees.

**Source:** https://staxpayments.com/pricing/ (page updated 2026-08-27)

**Design impact:** None. This affects cost modelling only.

**Stax written reply (verbatim):** _pending_

---

## Q2. Per-transaction and monthly volume limits; how to raise them

**Status:** PARTIAL. The mechanism is documented; **your account's actual numbers are not public** (PENDING).

**Public answer:**
- Each account has a per-transaction limit (called the "High Ticket" limit). Stax rejects any transaction above it with the error "The transaction total exceeds the transaction limit for this account."
- To raise it, submit a request with: Merchant ID, business or legal name, the 3 most recent bank statements, and the requested amount. Stax says it replies within one business day, and the update takes 1–2 business days.
  - This article is written for Stax Connect partners. A direct Stax Pay merchant should send the same information to Stax support.
- A third-party integrator (Service Fusion) states that limits are merchant-specific and set from the volume estimate given at signup. Processing far more than expected can trigger a review or a funds hold.
- One merchant review reports limits of $2,000 keyed-in and $4,000 card-present on their account. That is anecdotal and not your limit.

**Sources:**
- https://support.fattmerchant.com/requesting-to-increase-the-merchants-per-transaction-limit-high-ticket
- https://support.fattmerchant.com/common-transaction-errors
- https://servicefusion.zendesk.com/hc/en-us/articles/36573492162061-Overview-of-STAX-Processing-Limits

**Still needed from Stax:** your per-transaction limit (card and ACH separately, if they differ) and any monthly volume cap.

**Design impact:** Store the limit as a config value (`STAX_MAX_TXN_AMOUNT`) and warn before sending any invoice above it. Until Stax replies, **set it to $2,000 as a conservative placeholder**.

**Stax written reply (verbatim):** _pending_

---

## Q3. Webhook signature/secret, or only URL tokens? Published source IPs?

**Status:** PARTIAL. It leans toward "URL token only." Signature details and IPs are PENDING.

**Public answer:**
- Webhooks are created in the Stax Pay dashboard under Settings → Webhooks, or via API. You enter a URL, choose event types, and optionally a "secret key."
- The docs describe that secret key as something you "generate … in your URL." That makes it a secret token embedded in the endpoint URL, not a signing secret.
- The same page then says "If you set a secret key, validate the signature of the incoming requests," but **no signature header name, algorithm, or verification example is documented anywhere.**
- Stax's separate best-practices page recommends only one concrete mechanism: including tokens within the target URL.
- The POST body is the full object for the event; `create_transaction`, for example, sends the transaction object.
- A request header `stax-event-name` carries the event name, e.g. `stax-event-name: create_transaction`.
- **No published list of source IP addresses** was found.

**Sources:**
- https://docs.staxpayments.com/docs/setting-up-your-webhooks (updated 2026-05-09)
- https://docs.staxpayments.com/docs/setting-up-a-secure-webhook

**Still needed from Stax:** Is there any HMAC/signature header? If yes, what is its name and algorithm? Are source IPs published? What is the retry schedule for failed deliveries?

**Design impact (build this now, it is correct either way):**
- Secret token in the URL path, compared in constant time.
- Route on the `stax-event-name` header.
- **Re-fetch every object from the API by ID before acting;** never trust the payload.
- Deduplicate events, and run a nightly reconciliation job.
- If Stax later confirms a signature, add verification as an extra check without redesigning anything.

**Stax written reply (verbatim):** _pending_

---

## Q4. How customizable is the invoice email (subject, body, reply-to)?

**Status:** PENDING. Only account-level branding is documented.

**Public answer:**
- Documented settings: display name (Settings → Account), logo (Settings → Branding), business information shown on invoices and receipts, and allowed payment methods (Settings → Invoice Settings).
- Per invoice you can set a memo, hide line items on the customer page, enable partial payments or tipping, and choose card, ACH or both.
- "Customized invoicing" appears in the pricing page's feature list without detail.
- **Nothing public describes editing the email subject, body text, or reply-to address.**

**Sources:**
- https://portal.fieldcentral.com/knowledge/fieldcentral/stax-payment-links-setup (integrator guide to Stax settings)
- https://docs.staxpayments.com/reference/invoices
- https://staxpayments.com/pricing/

**Still needed from Stax:** Can subject, body and reply-to be customized per account or per invoice?

**Design impact:** Build assuming **Stax sends the email with account branding + per-invoice memo** (`PUT /invoice/{id}/send/email`). Put the human-readable invoice number and any custom message in the memo. Do not build a custom email sender in the MVP; doing so loses Stax's SENT/VIEWED tracking.

**TESTABLE:** Send a sandbox invoice to an internal inbox and record exactly what the email contains.

**Stax written reply (verbatim):** _pending_

---

## Q5. Does the hosted invoice page capture a Nacha-compliant ACH authorization?

**Status:** PENDING.

**Public answer:**
- ACH at Stax only accepts **US-based bank accounts**.
- ACH funding usually takes about **five days**, to limit clawbacks. Card payments fund the next business day.
- Nacha rules require the payer's authorization before an ACH debit. A third-party processor's guide says proof of authorization must be kept for **two years** after the last transaction.
- **Nothing public says whether Stax's hosted invoice page displays authorization language and stores the record on the merchant's behalf.**

**Sources:**
- https://docs.staxpayments.com/docs/ach-processing
- https://staxpayments.com/pricing/
- https://support.forte.net/support/solutions/articles/11000119211-proof-of-authorization-and-nacha-requirements (third-party, Nacha context)

**Still needed from Stax:** Does the hosted page capture ACH authorization text and consent? Who retains the proof of authorization, and can we retrieve it?

**Design impact:** None to the app's data model for now. If Stax does not retain proof of authorization, add a step later to store consent evidence. Treat ACH as "enabled but pending compliance confirmation" at go-live.

**TESTABLE (partially):** Open a sandbox invoice's payment page, choose bank payment, and screenshot any authorization text shown.

**Stax written reply (verbatim):** _pending_

---

## Q6. Human-readable invoice number / reference field behavior

**Status:** PARTIAL. Very likely supported; the exact field is TESTABLE in sandbox.

**Public answer:**
- Invoices are created with `POST /invoice`. Line items go in `meta.lineItems`, and a line item can reference a catalog item ID.
- The legacy developer guide says to pass a `url` for the Stax Pay payment page (`https://app.staxpayments.com/#/invoices`). The API appends the bill's ID to that URL and uses it in the email sent to the customer.
- Stax's Zapier "Create Invoice" action exposes a **Reference** field next to subtotal, tax and total. This strongly suggests a merchant-defined reference, probably `meta.reference`.
- Stax's own invoice ID is a system identifier, not a human-readable sequential number.

**Sources:**
- https://support.fattmerchant.com/developers-getting-started-with-stax-api
- https://zapier.com/apps/stax/integrations/agile-crm
- https://docs.staxpayments.com/reference/invoices

**Still needed:** Confirm the field name and whether it shows on the customer-facing email and page. **This does not need Stax. Test it in sandbox:** create an invoice with `meta.reference = "INV-TEST-0001"`, send it to yourself, and check the email, the payment page and the `GET /invoice/{id}` response.

**Design impact:** The app owns invoice numbering (`invoice_number`, unique). Send it to Stax as the reference and also include it in the memo, so it appears to customers even if the reference field is not displayed.

**Stax written reply (verbatim):** _pending_

---

## Q7. Can our server IP be whitelisted to avoid throttling during migration?

**Status:** ANSWERED (public).

**Public answer:**
- All API routes are limited to 200 requests per minute per IP, and 10 failed requests per minute per IP. Exceeding either blocks you for one hour.
- Stax.js payment calls are limited to 10 requests per minute.
- Stax invites merchants who need higher throughput to contact support or partner support to have their IP addresses whitelisted.

**New related finding:** A Stax help-center article on token migration says a separate **merchant-level** rate limit triggers after **50 failed transactions within one hour**, which blocks further requests from that merchant account. The article is written for Stax Connect partners; confirm whether it applies to direct Stax Pay merchants.

**Sources:**
- https://docs.staxpayments.com/reference/api-throttling
- https://support.fattmerchant.com/token-migration-process

**Still needed from Stax:** Request whitelisting for the production server's egress IP before the migration. This requires a **static egress IP**; serverless hosts such as Vercel do not provide one by default. Otherwise run the migration script from a machine with a fixed IP. Also confirm whether the 50-failed-transactions/hour limit applies to our account.

**Design impact:** The migration script throttles to about 1 request per second and stops on the first repeated error, whether or not whitelisting is granted. Count failed *payment* attempts too, not just failed API calls.

**Stax written reply (verbatim):** _pending_

---

---

## 2. Which answers change the design for the Rents ledger

| Q | Topic | Status | Design impact for Rents | Blocks what |
|---|---|---|---|---|
| Q2 | **Per-transaction limit** | PENDING (our number unknown) | Single rents reach **$12,581**; ~half of stores pay ≥ $3,800. A $2,000 limit would reject most debits. `STAX_MAX_TXN_AMOUNT` stays a config value; the monthly run must refuse to create any charge above it and list the refusals. | **Go-live of the monthly run.** Not Prompt 1–5. Ask Stax for the ACH limit specifically and request an increase to at least $15,000 with headroom for catch-ups. |
| Q3 | **Webhook signing / source IPs** | PARTIAL, leaning URL-token only | No change: URL token + re-fetch by ID + nightly reconciliation (`docs/CLAUDE.md` Rule 4). New detail: route on the `stax-event-name` header (TODO-VERIFY-015). | Nothing. |
| Q4 | **Email customization** | PENDING | Under D1-B (autopay) the email is a receipt, not a request to pay, so customization matters less. Under D1-A the operator must act on the email each month, so wording matters more. Put the store number and period in the memo/line item either way. | D1-A only. |
| Q5 | **ACH authorization (Nacha)** | PENDING | The business runs **merchant-initiated recurring ACH debits**. Nacha requires a retained authorization for exactly this. If Stax captures and retains it when the operator saves a bank method on the hosted page, we are covered. If not, we need a signed authorization per operator **before** enrolling them in autopay. | **Enrolment of operators in D1-B/C.** Ask Stax explicitly: "For recurring merchant-initiated ACH debits via scheduled invoices, do you capture and retain the Nacha authorization?" |
| Q1 | Card-not-present fee | Answered (public) | ACH at 1% capped at $10 makes ACH the only sensible channel for $3,800 rents. Card would cost ~$100 per rent. Supports ACH-only invoices (Q22). | Nothing. |
| Q6 | Invoice reference field | PARTIAL, testable | App owns the charge reference (app store id + external label + period, e.g. `S0412-2475-2026-09`, since labels are not unique). Send it in `meta` and in the memo so the operator's bank statement and email both show which store and month. Sandbox test in Prompt 2. | Nothing. |
| Q7 | IP whitelisting / rate limits | Answered (public) | Azure Container Apps egress through a NAT gateway gives a static IP; request whitelisting before migration. Migration of ~517 stores × ~18 months is local-DB only; Stax calls are limited to customer/payment-method lookups. | Nothing. |

## 3. Corrections and additions found on 2026-09-16

- **Rate limits.** The Stax "API Throttling" reference page (updated 2025-08-19) states verbatim:
  *"All Stax API routes are throttled to 200 requests per minute per IP address. You are only allowed
  10 failed API requests per minute per IP address. If you hit either of these limits, you will be
  blocked for one hour."* The separate "API Overview" page says 100 requests per minute with a
  `429`. Both are live. The app designs to the stricter combination: 100/min, 10 failed/min,
  assume a one-hour block. TODO-VERIFY-001 narrowed accordingly.
- **Merchant-level failed-transaction limit.** The token-migration help article's "50 failed
  transactions within one hour" limit is relevant to the monthly run (~47 returns a month, but those
  arrive days later; the risk is a burst of immediate validation failures). TODO-VERIFY-014.
- **Scheduled invoices.** Autopay via `payment_method_id`; without one the customer pays manually
  "and optionally save a payment method for future use." This is the basis of Decision D1-B.
- **Bank payment method creation.** The API endpoint that creates a bank payment method takes raw
  account and routing numbers. The app never calls it (Rule 1). Enrolment is on Stax's hosted page.

# docs/CLAUDE.md — Project Rules for the Stax Invoicing App

Read this file at the start of every session that touches this repository.
It holds the **project** rules: what the app is, what it must never do, and how it
talks to Stax Pay. The **process** rules (who plans, who writes code, QA gates,
secrets, publish stop gate) live in the root [`CLAUDE.md`](../CLAUDE.md) and are
not repeated here. Both files bind every session. Where they overlap, the stricter
rule wins.

Last verified against the Stax docs: **2026-09-10**. See §10 for what was checked.

---

## 1. What this app is

- Replaces a Google Sheets workflow for a small US business: customers, invoices,
  payments, notes, and internal follow-up status.
- Staff-only internal tool. Customers never log in here.
- Operators are debited by ACH through **Stax**, from a bank method that staff
  enter in the **Stax dashboard** (owner, 2026-09-29). Operators never enter bank
  details anywhere in this app, and this app never renders a payment form.
- Payment processor: **Stax Pay** (formerly Fattmerchant), Merchant API.
- Base URL: `https://apiprod.fattlabs.com`. Sandbox and live share this URL; only
  the API key differs (confirmed in Stax docs, "Test Environments").

### Two sources of truth

| Data | Source of truth | This app's role |
|---|---|---|
| Customers, line items, notes, internal workflow status, who-owes-what-by-when | **Our PostgreSQL database** | Owns and edits |
| Payment status, transactions, refunds, voids, deposits, disputes, Stax invoice status | **Stax** | Reads, caches, displays. Never writes. |

If a screen shows money state, it must be traceable to a Stax object fetched by ID,
with a `fetched_at` timestamp visible or one click away.

---

## 2. Stack (fixed, see `docs/DECISIONS.md` ADR-0001 and ADR-0002)

- Python 3.12+, Django (current LTS), PostgreSQL, Celery + Redis.
- Hosted on **Azure** (ADR-0002, 2026-09-27): Azure Container Apps for web, worker and
  beat; Azure Database for PostgreSQL Flexible Server; Azure Cache for Redis; secrets in
  Azure Key Vault read by managed identity. Owner's subscription, resource group
  `finance-prod` for live.
- Django admin is the staff UI for CRUD. Custom views only where admin is not enough.
- Celery beat runs scheduled jobs (nightly reconciliation). Celery workers run
  webhook re-fetches and all outbound Stax calls.
- Do not propose switching stacks. The decision is closed.

---

## 3. Ground rules

Each rule below has: the rule, why it exists, and how to apply it in code and review.
The QA gate in the root `CLAUDE.md` must check every one of these on every change
that touches payments, customers, invoices, webhooks, or jobs.

### Rule 1 — No card or bank account data, ever

**Rule.** No card number, CVV, expiry, bank account number, or routing number ever
enters this app. No input field for them. No column. No log line. No free-text field
that could reasonably hold one.

**Why.** Keeps the app out of PCI DSS and Nacha scope. One stored PAN turns a small
internal tool into a compliance liability.

**How to apply.**
- Bank methods are entered only in the Stax dashboard, by staff. This app links
  the resulting Stax customer ID and payment-method ID and nothing else. Do not
  embed Stax.js, do not build a "pay now" form, do not tokenize cards here. If a future prompt asks for an
  in-app payment form, stop and escalate; it conflicts with this rule.
- Free-text fields (notes, memos, descriptions) get a server-side validator that
  rejects strings matching a 13–19 digit run that passes the Luhn check, and
  9-digit ABA routing patterns adjacent to a longer digit run. Reject with a
  value-free error ("Notes may not contain card or account numbers").
- Never log request or response bodies from Stax verbatim. Log IDs, status codes,
  and our own correlation ID only. If a Stax response includes a `payment_method`
  object, keep at most `id`, `method`, `card_type`, `card_last_four`,
  `card_exp` (month/year), and `nickname`. Drop everything else before persisting.
- Test fixtures use Stax's published sandbox test cards only, and only inside
  documented sandbox test flows. Never in a unit test that runs against our DB.
- **Uploaded files (ADR-0008, PLAN-010c).** Documents on stores and leases live in a
  private blob container and are subject to this rule like every text field: the text of
  each PDF and DOCX is extracted at upload and run through the same detectors; a hit
  refuses the file and stores nothing; a PDF that cannot be read is refused. Images and
  scanned PDFs cannot be checked, so the rule for them is policy, shown on every upload
  box: **no bank forms, ACH authorisations, voided cheques or card details; those stay in
  Stax or DocuSign.** Do not add OCR to "close" this gap; it would bring the data in.
  A file found to break the rule is deleted by an admin and the deletion is recorded.

### Rule 2 — Stax is the source of truth for payment state

**Rule.** The app reads payment state from Stax and never writes it. Stax invoice
statuses are system-managed and cannot be set through the API. Internal workflow
statuses live in separate columns in our own database.

**Why.** Confirmed in Stax docs ("Invoices" reference): *"Invoice statuses in the
Stax platform are automatically managed by the system and cannot be manually
modified via API requests."* Trying to mirror or override them creates two
disagreeing ledgers.

**How to apply.**
- Two distinct fields on our invoice model, never merged:
  - `provider_status` (named `stax_status` before PLAN-004; cached copy, read-only in admin, values exactly as Stax returns
    them): `DRAFT`, `SENT`, `VIEWED`, `PAID`, `PARTIALLY APPLIED`, `ATTEMPTED`,
    `DELETED`. The docs also mention `PARTIALLY PAID` in one transition list;
    treat unknown values as opaque strings and store them as-is (TODO-VERIFY-003).
  - `internal_status` (named `workflow_status` before PLAN-004; ours, editable; final list
    `draft`, `ready`, `sent`, `closed`, `void`). Earlier examples: e.g. `draft`, `ready_to_send`, `sent`,
    `follow_up`, `disputed`, `written_off`, `closed`. Define the final list in the
    first data-model prompt.
- The only Stax calls that change money state and are allowed from this app are:
  send invoice via email/SMS, mark invoice paid in cash/check (a staff action that
  records an offline payment), and refund/void a transaction. Each of these is a
  deliberate staff action behind a confirmation step and an audit log row. No job
  or webhook handler may trigger them.
- Collection model is **D1-B** (`docs/SCOPE.md` §4, owner decision 2026-09-27):
  one Stax **invoice schedule** per lease debit day with autopay from a bank
  method that staff entered in the Stax dashboard (owner, 2026-09-29). Annual
  insurance is a separate yearly schedule (ADR-0005). The allowed write calls
  therefore include create, edit, pause and delete of an invoice schedule. Each is
  a deliberate staff action or a reviewed migration step, never a webhook side
  effect. Invoices are ACH-only with no card exception. The full amount is always
  due. **A schedule's amount is never edited for an adjustment:** each adjustment
  is its own one-off invoice linked to a `charge_adjustments` row
  (TODO-VERIFY-019).
- "Pay an invoice" (`POST /invoice/{id}/pay`) is **not used**. If
  TODO-VERIFY-019 shows a one-off invoice cannot be collected without it, that
  is an owner decision recorded in `docs/DECISIONS.md` first, not a code change. The app never
  creates a payment method (Rule 1).
- Cached Stax data always carries `fetched_at`. Any decision logic (overdue,
  follow-up due) uses the cache but shows the staleness.

### Rule 3 — Rate limits are a business risk

**Rule.** All Stax traffic goes through **one** rate-limited, circuit-broken client.
Never retry a 4xx in a loop. Treat an IP block as an outage.

**The numbers.** The Stax "API Throttling" reference page (checked 2026-09-16)
states 200 requests/minute per IP, 10 **failed** requests/minute per IP, and a
**one-hour block** on breach. The separate "API Overview" page states **100
requests per minute** with a `429 Too Many Requests` response. Both pages are
live. Until the support ticket says which is enforced (TODO-VERIFY-001), design
to the **stricter** combination:

| Limit | Design value | Source |
|---|---|---|
| Requests per minute per IP | **100** | API Overview page |
| Failed requests per minute per IP | **10** | API Throttling page |
| Penalty | **One-hour IP block** | API Throttling page |

**Why.** The Container Apps environment egresses through one NAT gateway IP. A block
on it takes invoicing offline for an hour with no override. A bug that retries a 400 ten
times in a minute is enough to cause it.

**How to apply.**
- One module owns the HTTP session: the Stax adapter. Nothing else imports
  `requests`/`httpx` for Stax. Enforce with a lint rule or a test that greps.
- Token bucket in Redis: **60 requests/minute** steady state (60% of the 100
  ceiling, headroom for retries and staff clicks), burst of 10. Bucket key is
  per-environment so sandbox and live do not share a bucket.
- Failure budget in Redis: a sliding window of non-2xx responses. At **5 failures
  in 60 seconds** (half of the assumed 10), the circuit **opens** for 10 minutes:
  all Stax calls fail fast locally with a `ProviderUnavailable` error, jobs are
  requeued with backoff, and an alert is raised. Half-open after 10 minutes with
  a single probe (a cheap `GET`).
- Retry policy, no exceptions:
  - `429`: honour `Retry-After` if present, otherwise wait 60 seconds. Max 1 retry.
  - `5xx` and network timeouts: exponential backoff starting at 2 seconds,
    max 3 attempts, jittered.
  - **Any other 4xx (400, 401, 403, 404, 409, 422): never retry.** Record it, fail
    the task, surface it to a human.
- Nightly reconciliation paginates with a fixed per-page delay so a full run of
  the customer and invoice lists stays under the steady-state budget. Compute the
  expected request count before the run and abort if it would exceed 50% of the
  hourly budget.
- Every Stax call logs: method, path (IDs redacted to type), status, latency,
  correlation ID, bucket remaining, breaker state. Never the body.
- A staff-visible status page shows breaker state and bucket level.

### Rule 4 — Webhooks are unsigned hints

**Rule.** Stax documents no HMAC signature for webhooks. The only documented
protection is a secret token in the webhook URL. Treat every webhook as an
untrusted hint: verify the URL token, then re-fetch the object from the Stax API by
ID before changing anything.

**Why.** Confirmed in Stax docs ("Webhook Best Practices"): *"You can make your
webhook more secure by including tokens within the target URL."* Nothing about
signatures or published source IPs. Anyone who learns the URL can post a fake
payload. TODO-VERIFY-002 tracks the support-ticket question on signing and IPs.

**How to apply.**
- Webhook path: `/webhooks/stax/<token>/` where `<token>` is a 64+ character random
  value from the `STAX_WEBHOOK_URL_TOKEN` env var. Compare with a constant-time
  comparison. Wrong token → `404` (not `401`, do not confirm the route exists).
- The view does exactly three things: check the token, extract the event name and
  object ID from the payload, enqueue a Celery task. It returns `200` in under
  100 ms. It **never** touches the database except to write a raw
  `webhook_receipt` row (event name, object ID, received_at, correlation ID,
  payload hash — not the payload body).
- The Celery task re-fetches the object by ID through the `PaymentProvider`
  (`GET /invoice/{id}`, `GET /transaction/{id}`, `GET /customer/{id}`, etc.) and
  updates our cache from **that** response. The webhook body is never trusted as
  data, even though Stax says it matches the GET response.
- Idempotent handling: the same event for the same object arriving twice must
  produce the same end state. Key the task on `(event, object_id, updated_at)`.
- Rate-limit awareness: webhook re-fetches go through the same token bucket. A
  burst of webhooks (e.g. a bulk send) must not starve staff actions; use a
  lower-priority Celery queue for webhook re-fetches.
- Events we subscribe to (names exactly as in the Stax "Merchant Webhook Events"
  page): `create_invoice`, `update_invoice`, `send_invoice`,
  `create_transaction`, `update_transaction`, `update_transaction_settled`,
  `create_deposit`, `create_dispute`, `update_dispute`, `create_customer`,
  `update_customer`. Ignore all others with a `200` and a log line.
- Webhooks are a latency optimisation, not the sync mechanism. The nightly
  reconciliation (Rule 3) is what guarantees correctness if webhooks are missed.

### Rule 5 — Creation is not idempotent; prevent duplicates locally

**Rule.** Invoice and customer creation are NOT idempotent in Stax. Only
`POST /charge` and `POST /invoice/{id}/pay` accept an `idempotency_id`. Prevent
duplicates with a local-first create plus a check-before-create on retry.

**Why.** Confirmed in Stax docs ("Idempotency"): the `idempotency_id` parameter is
supported by Stax.js `.pay`, `POST /charge`, and `POST /invoice/{id}/pay` only.
A timeout on `POST /invoice` may or may not have created the invoice. Blind retry
creates a duplicate the customer can pay twice.

**How to apply.**
- **Local first.** Create the row in our DB with `sync_state = pending_create`
  and a unique `local_ref` (UUID) **before** calling Stax. Commit that transaction.
- **Carry the local ref.** Put `local_ref` into a Stax field we can search on
  later. Candidate fields per the spec must be confirmed before use
  (TODO-VERIFY-004: which of `meta`, `reference`, or a memo field is searchable
  or at least returned on `GET`). Until confirmed, `meta.local_ref` is the working
  assumption.
- **Check before create on retry.** If the create call failed with a timeout,
  `5xx`, or an ambiguous result, do **not** call create again. Instead:
  1. `GET` the list endpoint filtered as narrowly as the spec allows (customer:
     by email; invoice: by customer ID and recent date range), page through,
     look for our `local_ref`.
  2. If found: record the Stax ID in `external_refs`, set `sync_state = synced`.
  3. If not found after the search: allow one more create attempt, then stop and
     mark `sync_state = needs_review` for a human.
- A `4xx` on create is never retried (Rule 3). Mark `needs_review`.
- Customer duplicates are also a business problem: before creating a customer in
  Stax, search our own DB by normalised email and phone and show staff the match.
  Stax has its own "Customer Matching" behaviour (see docs); note it, but do not
  rely on it.
- Nightly reconciliation lists Stax objects and flags any that carry a
  `local_ref` we do not have, or that we have twice.

### Rule 6 — Sandbox and live must be impossible to confuse

**Rule.** Sandbox and live share `https://apiprod.fattlabs.com`. The environment
must be impossible to confuse: a `STAX_ENV` variable checked at boot, and a
persistent on-screen SANDBOX banner.

**Why.** There is no URL difference to catch a mistake. The only signal is which
key is loaded. A live key in a test run sends real invoices to real customers.

**How to apply.**
- `STAX_ENV` must be exactly `sandbox` or `live`. Anything else, including unset,
  aborts startup of the web process, every Celery worker, and beat, with a
  value-free error.
- `STAX_API_KEY` is required. The app never falls back to a default.
- Key/environment cross-check at boot: call one cheap read endpoint (confirm which
  in the spec, TODO-VERIFY-005; `GET /self` or the merchant/team endpoint is the
  usual candidate) and compare the returned merchant name or ID to
  `STAX_EXPECTED_MERCHANT_ID`. Mismatch aborts startup. This catches a live key
  pasted into a sandbox deploy and vice versa.
- Banner: when `STAX_ENV=sandbox`, every rendered page, including Django admin,
  shows a fixed, high-contrast **SANDBOX** bar at the top that cannot be
  dismissed. Page `<title>` is prefixed `[SANDBOX]`. Emails the app itself sends
  (if any) carry `[SANDBOX]` in the subject.
- Separate resource groups, databases, Redis instances and Container Apps
  environments per environment. Vault secret names carry the environment
  (`stax-api-key-sandbox`, `stax-api-key-live`). Never one deploy that can switch
  environments at runtime.
- Local development defaults to `sandbox` only via `.env.example` guidance, never
  via code. The code has no default.
- Test suite: a test asserts the app refuses to start with `STAX_ENV` unset, and
  one asserts the banner is present on the admin index when `sandbox`.

### Rule 7 — Read the spec before writing any Stax call

**Rule.** Before writing any Stax call, read the endpoint in the official spec.
Never invent field names. If a field cannot be confirmed in the spec, mark it
`TODO-VERIFY` in code and list it in `docs/TODO-VERIFY.md`.

**Where the spec is.**
- Index: `https://docs.staxpayments.com/llms.txt`
- API reference index: `https://docs.staxpayments.com/reference/llms.txt`
- Guides index: `https://docs.staxpayments.com/docs/llms.txt`
- Any page URL + `.md` returns Markdown. Fetch the `.md` version; the HTML
  summaries drop field tables.
- Note: the top-level `llms.txt` lists only section indexes. Fetch the section
  `llms.txt` to get page URLs.

**How to apply.**
- Every adapter method has a docstring with the exact spec URL(s) it implements
  and the date read.
- Field names, enum values, pagination parameters, and filter parameters come
  from the spec page for that endpoint, copied verbatim. If the page is
  ambiguous, use `# TODO-VERIFY-nnn: <question>` in code and add a row to
  `docs/TODO-VERIFY.md` in the same change. QA blocks a change that has a
  `TODO-VERIFY` comment without a matching register row.
- Do not rely on memory of the Fattmerchant API, on third-party SDKs, or on blog
  posts. The docs changed brand and structure; older field names may be gone.
- When a sandbox call returns a field not in the spec, record it in
  `docs/TODO-VERIFY.md` as an observation. Do not depend on it.

### Rule 8 — One `PaymentProvider` interface, one `external_refs` table

**Rule.** All Stax calls sit behind a `PaymentProvider` interface. No Stax-specific
code outside the adapter. External IDs live in an `external_refs` table, not in
`stax_*` columns scattered across tables.

**Why.** Isolates the processor so a second developer can read one package to
understand every outbound call, and so rate limiting, logging, and the circuit
breaker are applied in exactly one place. A separate refs table keeps the domain
model processor-agnostic and supports one local object having several external
IDs over time (e.g. a re-created invoice after a Stax-side delete).

**How to apply.**
- Package layout (to be created in the first code prompt, by Opus, from a Fable
  plan):
  - `backend/payments/provider.py` — the abstract `PaymentProvider` with typed
    request/response dataclasses that use **our** vocabulary, not Stax's.
  - `backend/payments/stax/` — the only place that knows about Stax URLs,
    headers, field names, and status strings. Contains the rate limiter, circuit
    breaker, and retry policy.
  - `backend/payments/fake.py` — an in-memory provider for tests and local dev.
- Import rule: nothing outside `backend/payments/stax/` may import from it, and
  nothing outside `backend/payments/` may mention the string "stax" in code
  except settings and the banner. Enforce with an import-linter contract or a
  test.
- `external_refs` table: `(id, entity_type, entity_id, provider, external_id,
  external_kind, created_at, superseded_at NULL)` with a unique index on
  `(provider, external_kind, external_id)` and a partial unique index on
  `(entity_type, entity_id, provider, external_kind) WHERE superseded_at IS NULL`.
- Cached Stax payloads (trimmed per Rule 1) go in a `provider_snapshots` table
  keyed by `external_refs.id`, with `fetched_at`. Domain tables hold only the
  small denormalised fields the UI needs (`provider_status`, `amount_paid` as exact decimals,
  `last_payment_at`), each refreshed from a snapshot, never edited by hand.

---

## 4. Environment variables

All read at runtime, never at build time. All required unless marked optional.
Document every new one in `.env.example` with an empty value (root `CLAUDE.md` §5.3).

**Secret storage:** every secret below lives in **Azure Key Vault** (`finance-kv` for
live) and reaches the app through the container apps' managed identity at runtime
(ADR-0002). Models never read vault values (root `CLAUDE.md` §5.1). Sandbox and live
keys are separate secrets with the environment in the name (Rule 6). **The key stored
on 2026-09-16 is the LIVE key** (owner, 2026-09-27); it is to be named
`stax-api-key-live` and must not be readable by any development identity. **No
sandbox key exists yet.** Until `stax-api-key-sandbox` exists, no session may make
any Stax call, and no code may be tested against Stax.

| Name | Purpose |
|---|---|
| `APP_ENV` | `local`, `staging` or `production`: which deployment this is. Distinct from `STAX_ENV`. |
| `STAX_ENV` | `sandbox` or `live`. Boot aborts on anything else, in every `APP_ENV`. `live` is refused unless `APP_ENV=production`. |
| `STAX_API_KEY` | Merchant API key for that environment. Vault secrets `stax-api-key-sandbox` / `stax-api-key-live`. |
| `STAX_API_BASE_URL` | Always `https://apiprod.fattlabs.com`; the guard enforces the exact value outside `local`. Overridable locally only so tests can point at a fake. |
| `STAX_EXPECTED_MERCHANT_ID` | Boot cross-check against the key (Rule 6). Wired in the adapter prompt, not before. |
| `STAX_WEBHOOK_URL_TOKEN` | Secret path segment for inbound webhooks (Rule 4), 64+ characters. Vault secrets `stax-webhook-url-token-<env>`. |
| `STAX_RATE_LIMIT_PER_MINUTE` | Optional. Default 60. Never set above the documented ceiling. |
| `APP_BASE_URL` | Public URL of this deployment; drives allowed hosts and CSRF origins. |
| `SENTRY_DSN` | Optional. Empty disables Sentry. PII scrubbing is always on. |
| `DATABASE_URL`, `REDIS_URL`, `DJANGO_SECRET_KEY` | Standard Django/Celery. SQLite is allowed only when `APP_ENV=local`. |
| `AZURE_KEY_VAULT_URL` | Vault to read the secrets above from at runtime, via managed identity. Locally unset; values come from `.env`. |

**DNS (owner, 2026-09-27):** the public Azure DNS zone **`monvera.io`** lives in resource group
`finance-prod`. Any hostname the app needs is a record in that zone. Proposed names, to be
confirmed by the owner before the first deployment: `staging.monvera.io` for the staging
environment and `app.monvera.io` for production. `APP_BASE_URL` is set to `https://<host>`
accordingly. The staging Bicep in a different resource group needs a cross-resource-group
reference to the zone (or the owner creates the CNAME/TXT records by hand); both are Section 6
publish actions and need explicit approval.

---

**Added by PLAN-005 (2026-09-30):** `STAX_CALLER` (`none` default, `worker` for the single
process that may call Stax), `STAX_PAY_INVOICE_ENABLED` (`false` default; TODO-VERIFY-019),
`STAX_EXPECTED_MERCHANT_ID` (now read by `provider_ping`), and `STAX_SANDBOX_API_KEY` (read
only by the sandbox test suites; secret). See `docs/STAX_CLIENT.md`.

## 5. Things this app deliberately does not do

- Collect or tokenize payment details (Rule 1).
- Set or override any Stax status (Rule 2).
- Call `POST /charge` or `POST /invoice/{id}/pay` (Rule 2, Rule 1).
- Trust a webhook body as data (Rule 4).
- Retry a 4xx (Rule 3).
- Run against live and sandbox from one deploy (Rule 6).
- Send email itself for invoices. Stax sends the receipt after a payment. If the business later wants custom email, that is a new decision.

---

## 6. Session start checklist

1. Read root `CLAUDE.md` (process) and this file (project).
2. Read `docs/DECISIONS.md` and `docs/TODO-VERIFY.md`.
3. If the task touches a Stax endpoint, fetch its `.md` spec page first and note
   the URL in the plan.
4. Confirm which environment any manual test will hit. Default to none; sandbox
   only with the sandbox key; live never from a development session.
5. Apply the root `CLAUDE.md` role split: Fable plans, reviews, tests, documents.
   Opus writes production code. Neither publishes without an explicit
   Publish Request approval.

---

## 7. Definitions

- **Stax status**: the value Stax returns on an invoice or transaction. Read-only.
- **Workflow status**: our internal follow-up state. Editable by staff.
- **Local ref**: our UUID for an object, generated before any Stax call.
- **External ref**: a Stax ID, stored only in `external_refs`.
- **Snapshot**: a trimmed copy of a Stax object with `fetched_at`.
- **Reconciliation**: the nightly job that lists Stax objects and repairs our cache
  and refs. It is the correctness mechanism; webhooks are an optimisation.

---

## 8. Open questions sent to Stax support (checkpoint 0)

Tracked in `docs/TODO-VERIFY.md`. Summary: card-not-present fee; per-transaction and
monthly limits; whether webhooks are signed or URL-token only and whether source IPs
are published; invoice email customisation; what ACH debit authorisation Stax requires when staff enter the bank method
(the hosted page is not used); behaviour of the human-readable invoice reference field; whether
our server IPs can be whitelisted for migration. Rate-limit figures were added to
that list on 2026-09-10 because the docs and the founding prompt disagree.

---

## 9. Amending this file

- Any change to §3 (ground rules) needs a new entry in `docs/DECISIONS.md`.
- Resolving a `TODO-VERIFY` item: update the relevant rule text here, close the row
  in `docs/TODO-VERIFY.md`, and remove the code comment, all in one change.
- The human owner reviews this file at each checkpoint. Models may propose edits
  but must call them out explicitly in the session summary.

---

## 10. What was verified against the Stax docs on 2026-09-10

| Claim | Source page | Result |
|---|---|---|
| Sandbox and live share `https://apiprod.fattlabs.com` | `/docs/test-environments.md` | Confirmed verbatim |
| Invoice statuses are system-managed, not settable via API | `/reference/invoices.md` | Confirmed verbatim |
| Invoice status values | `/reference/invoices.md` | `DRAFT`, `SENT`, `VIEWED`, `PAID`, `PARTIALLY APPLIED`, `ATTEMPTED`, `DELETED`; transition text also says `PARTIALLY PAID` (TODO-VERIFY-003) |
| Only `POST /charge`, `POST /invoice/{id}/pay`, and Stax.js `.pay` accept `idempotency_id` | `/docs/idempotency.md` | Confirmed |
| Webhook protection is a token in the URL; no signature documented | `/docs/setting-up-a-secure-webhook.md` | Confirmed; no HMAC or IP list anywhere in the webhook pages |
| Webhook payload equals the `GET` response for the object | `/docs/merchant-webhooks.md` | Stated by Stax; we still re-fetch (Rule 4) |
| Merchant webhook event names | `/docs/merchant-webhooks.md` | Listed in Rule 4 |
| Rate limit | `/reference/overview.md` | **100 requests/minute**, `429` + `Retry-After` |
| Rate limit, failed-request limit, one-hour block | `/reference/api-throttling.md` (read 2026-09-16) | **200 requests/minute per IP, 10 failed requests/minute per IP, one-hour block.** Conflicts with the overview page; design to the stricter combination (TODO-VERIFY-001) |
| Scheduled invoices support autopay via `payment_method_id`; payer may save a method on the hosted page | `/reference/scheduled-invoices.md` (read 2026-09-16) | Confirmed; basis of `docs/SCOPE.md` Decision D1-B |
| `POST /invoice/{id}/pay` takes `payment_method_id` and `idempotency_id` | `/reference/pay-an-invoice.md` (read 2026-09-16) | Confirmed |
| ACH lifecycle: `success: true` at creation, batch 8 pm ET, settle or void within ~5 business days, `update_transaction_settled` fires either way, Nacha code in `child_transactions[0].meta.achDescription` | `/docs/bank-ach-transactions.md` (read 2026-09-16) | Confirmed |

# Architecture Decision Records

One entry per decision. Newest at the bottom. Entries are never deleted; a
reversed decision gets a new entry that supersedes the old one.

Format: context, decision, alternatives rejected, consequences, status.

---

## ADR-0001 — Application stack: Django + PostgreSQL + Celery on Render

**Date:** 2026-09-10
**Status:** Accepted (confirmed by the project owner in session)
**Deciders:** Project owner; recommendation prepared by Fable

### Context

A single developer, working with an AI assistant, is building a staff-only
internal app to replace a Google Sheets invoicing workflow and integrate with
Stax Pay's Merchant API. Hosting budget is roughly $45–$95 per month. The app
needs inbound webhooks, a nightly reconciliation job, and, above all, a single
rate-limited and circuit-broken client for every outbound Stax call, because an
IP block would take invoicing offline (see `docs/CLAUDE.md` Rule 3). Customers
pay on Stax-hosted pages, so there is no customer-facing UI.

Three candidates were scored 1–5 on: build speed with an AI assistant, webhook
and cron support, cost in budget, ease of handover to a second developer, and
long-term maintenance.

| Criterion | (a) Next.js + Supabase + Vercel | (b) Django + Postgres + Celery + Render | (c) Low-code UI + serverless backend |
|---|---|---|---|
| Build speed with AI assistant | 5 | 4 | 3 |
| Webhook and cron support | 3 | 5 | 3 |
| Cost at $45–$95/month | 5 (~$45) | 4 (~$50–$75) | 3 (~$60–$100, grows per seat) |
| Handover to a second developer | 4 | 5 | 2 |
| Long-term maintenance | 3 | 5 | 2 |
| **Total** | **20** | **23** | **13** |

### Decision

Build on **Python 3.12+, Django (current LTS), PostgreSQL, Celery with Redis**,
hosted on **Render** (web service, worker service, beat, managed Postgres,
managed Redis). Django admin is the staff UI; Celery beat runs the nightly
reconciliation; a Celery worker executes all outbound Stax calls and all webhook
re-fetches.

### Why

- The rate limiter and circuit breaker need shared, long-lived state. A
  persistent worker plus Redis is the natural home for a token bucket and a
  failure window. On a serverless platform each invocation is a fresh process,
  so that state would have to be rebuilt in the database, which is fragile and
  is exactly where a mistake gets the IP blocked.
- Django admin provides the customer, invoice, notes, and workflow-status CRUD
  screens with almost no code, and the mandatory SANDBOX banner is one template
  override.
- Webhook handling is a plain view that verifies the URL token and enqueues a
  task; there is no cold-start or execution-time ceiling to design around.
- Render offers static outbound IPs on paid plans, which is relevant to the
  open support question about IP whitelisting.
- The stack is on LTS releases with low churn, and any Python developer can take
  it over without learning a platform-specific runtime.

### Alternatives rejected

**(a) Next.js (App Router) + TypeScript + PostgreSQL (Supabase) + Vercel.**
Fastest to build with an AI assistant and cheapest at about $45/month. Rejected
because serverless functions make the single stateful Stax client harder and
riskier: limiter and breaker state must live in Postgres or an external store,
Vercel Cron granularity and function duration limits shape the nightly job, and
a second developer inherits a platform-specific runtime with more framework churn.
Its UI advantage is irrelevant because customers never use this app.

**(c) Hybrid: low-code staff UI + small serverless backend owning all Stax calls.**
Rejected because it splits business logic and permissions across two systems,
tempts the low-code tool to talk to the database directly and bypass the
`PaymentProvider` boundary, has the same serverless state problem as (a), costs
scale per seat, and it is the hardest to hand over or to drive with an AI
assistant.

### Consequences

- Repository layout will be a single Django project under `backend/`, with
  `backend/payments/` holding the `PaymentProvider` interface and the Stax
  adapter (see `docs/CLAUDE.md` Rule 8).
- Two Render environments (sandbox, live), each with its own web, worker, beat,
  Postgres, and Redis. No shared resources between them.
- Expected hosting cost is roughly $50–$75/month for the live environment. The
  sandbox environment can run on free or starter tiers.
- The staff UI will be plain Django admin plus a few custom views. If a richer UI
  is ever wanted, add HTMX or a small frontend; do not change the backend stack.
- Model roles from the root `CLAUDE.md` apply: Fable plans, reviews, tests, and
  documents; Opus writes production code.

---

## ADR-0002 — Hosting and data on Azure (supersedes the hosting part of ADR-0001)

**Date:** 2026-09-27
**Status:** Accepted (owner statement, 2026-09-27: "all our data and hosting needs will be done on Azure")
**Supersedes:** the "hosted on Render" clause of ADR-0001. The application stack in ADR-0001
(Python 3.12+, Django LTS, PostgreSQL, Celery with Redis) is unchanged.

### Context

The owner already holds an Azure subscription and resource group named `finance-prod` with a
Key Vault `finance-kv`, and stored the Stax API key there on 2026-09-16. Keeping secrets in Key
Vault only works as a runtime source if the app runs on Azure and reads the vault by managed
identity. The owner has now confirmed that hosting and data live on Azure.

### Decision

Run the Django stack on Azure, in the owner's subscription, using managed services only:

| Component | Azure service | Notes |
|---|---|---|
| Web (Django + admin) | **Azure Container Apps** | One container app. Static outbound IP via a NAT gateway on the Container Apps environment, so the address can be given to Stax for whitelisting. |
| Celery worker and beat | Azure Container Apps | Same image, different commands. Beat runs as a single replica. |
| PostgreSQL | **Azure Database for PostgreSQL, Flexible Server** | Burstable tier to start. Private access, no public endpoint. |
| Redis (rate limiter, breaker, Celery broker) | **Azure Cache for Redis**, Basic tier | Separate instance per environment. |
| Secrets | **Azure Key Vault** `finance-kv` | Read at runtime by the container apps' **user-assigned managed identity**. No client secrets, no keys in environment files. |
| Container images | Azure Container Registry | Built by CI, pulled by managed identity. |
| Logs and alerts | Azure Monitor / Log Analytics | Breaker-open and 429 alerts go here. |

Two isolated environments, `sandbox` and `live`, each with its own resource group, database,
Redis, Container Apps environment and vault secrets. Nothing is shared between them
(`docs/CLAUDE.md` Rule 6).

Naming of vault secrets carries the environment: `stax-api-key-sandbox`, `stax-api-key-live`,
`stax-webhook-token-sandbox`, `stax-webhook-token-live`. The resource group `finance-prod` holds
the live environment; the sandbox environment gets its own resource group (proposed name
`finance-sandbox`). If the owner prefers one resource group, the sandbox resources still get
their own vault or their own secret names, and the boot check in Rule 6 must still pass.

### Why Container Apps rather than App Service or a VM

- Runs web, worker and beat from one image with per-process scaling, which App Service does
  not do cleanly for Celery.
- Managed identity, Key Vault references and private networking are first-class.
- Scale-to-zero is not used (beat must run), but the minimum-replica cost is within budget.
- No VM to patch, which matters for a single-developer project.

### Alternatives rejected

- **Stay on Render and copy secrets by hand.** Rejected: the owner wants data and hosting on
  Azure, and hand-copied secrets defeat the point of a vault.
- **Azure App Service for Linux.** Workable for the web tier, awkward for Celery worker and beat
  (WebJobs or a second plan). Rejected for operational simplicity.
- **Azure Functions / serverless.** Same objection as ADR-0001 option (a): the rate limiter and
  circuit breaker need long-lived shared state and a persistent worker.
- **A single VM.** Cheapest, but patching, backups and TLS become the developer's job.

### Consequences

- `docs/CLAUDE.md` §2 and §4 are updated in the same change.
- Estimated cost for the live environment: roughly $70–$110 per month (Container Apps ~$30,
  PostgreSQL Burstable B1ms ~$15–$25, Redis Basic C0 ~$16, NAT gateway ~$35, Key Vault and
  registry a few dollars). This is at or slightly above the original $45–$95 target; the NAT
  gateway is the main extra and exists only to give Stax a fixed IP. Owner accepts or drops
  whitelisting.
- Sandbox environment: same shape at the smallest tiers, or the developer's machine plus a
  sandbox database, at the owner's choice.
- Infrastructure as code in `infra/` (Bicep) is written by Opus from a Fable plan in a later
  prompt. Deployment is a Publish action under root `CLAUDE.md` §6 and needs explicit approval.
- Models never read vault values (root `CLAUDE.md` §5.1). Local development uses a `.env` the
  owner fills by hand from the vault; `.env` is git-ignored.

---

## ADR-0003 — `/healthz` is a public route

**Date:** 2026-09-28
**Status:** Accepted (owner decision, 2026-09-28)

### Context

Prompt 3 fixes the public surface as the login page and, later, the webhook receiver, and
tells the engineer to stop and ask before making anything else public. Azure Container Apps
readiness and liveness probes and the CI smoke test call the health endpoint without a
session and cannot complete an MFA login.

### Decision

`GET /healthz` is public. It returns only `{"status": "ok", "app_env": ..., "stax_env": ...}`.
It never returns a secret, a hostname, a version string, a database status message, or any
data from the application tables. Any method other than GET is refused by the role registry.
It is exempt from the HTTPS redirect so in-replica probes over plain HTTP get 200.

### Alternatives rejected

- Authenticated health check: probes cannot authenticate.
- Probe by TCP only: would report a process that is up but misconfigured as healthy, and the
  CI smoke test would lose its check that staging reports `stax_env=sandbox`.

### Consequences

- The public set is exactly: two-factor login flow, `/static/`, `/healthz`, and from Step 8 the
  webhook receiver at its token path. `backend/tests/test_route_registry.py` asserts this set.
- Adding any field to the health payload needs a review against this record.

---

## ADR-0004 — Scheduled invoices are MVP scope; prompt series amended for the Rents ledger

**Date:** 2026-09-29
**Status:** Accepted (follows from Decision D1, confirmed by the owner in writing on 2026-09-29; amendments supplied by the owner in `docs/inputs/PROMPT_SERIES_AMENDMENTS.md`). **Updated 2026-09-29, see "Owner statements of 2026-09-29" below.**

### Context

The prompt series was written for "a customer receives an invoice and pays it on a hosted
page". The Rents audit showed the business debits about 500 operators' bank accounts by ACH
on a fixed day each month. Decision D1 (`docs/SCOPE.md` §4) chose Stax scheduled invoices with
autopay. Prompt 12 of the series lists "scheduled (recurring) invoices" as a Phase 7 backlog
item, which contradicts D1.

### Decision

1. **Scheduled invoices via Stax's scheduled-invoice endpoints are MVP scope.** The Phase 7
   backlog item for them is deleted. When Prompt 12 is run, its backlog list omits it.
2. The amendments in `docs/inputs/PROMPT_SERIES_AMENDMENTS.md` are applied as each prompt comes
   up. In summary: Prompt 4 gains `stores`, `operators`, `leases`, `lease_rent_history`,
   `rent_charges` and payment states `pending`/`returned`; Prompt 5's provider interface gains
   the scheduled-invoice and ACH-rejection methods; Prompt 6 treats an operator without an email
   as valid but not eligible for autopay onboarding; Prompt 7 becomes lease setup plus the
   monthly rent run; Prompt 8 adds the ACH return path; Prompt 9 adds wire and a returns
   worklist; Prompt 10 reconciles ACH rejections; Prompt 11 must include the month grid;
   Prompt 13 imports history as local-only legacy rent charges; Prompt 16 cuts over per operator.
3. Where the amendments file and later owner decisions differ, **the later decision wins**:
   - **Corrected 2026-09-29 by the owner:** questions 1, 2 and 5 (positive = collected,
     negative = not collected, Rec. Date = debit day) are **not** answered by the owner. They are
     data-derived with status STRONG in `docs/inputs/ANSWERS_ALL_25.md` and remain **pending
     owner confirmation in writing**. The amendments file's blocking list (questions 1–3 and 6,
     the ACH limit, operator emails, sheet version history) **stands in full** until the owner
     confirms. The import must not be written on these assumptions as if they were settled.
   - The amendments' remarks about serverless hosts do not apply: the app runs on Azure
     Container Apps with a persistent worker and a NAT gateway with a static egress IP
     (ADR-0002). The rate limiter is global through Redis, to be provisioned at Step 10.
   - Data model names fixed in `docs/SCOPE.md` §5 (`lease_rents`, `debit_days` list,
     `collection_status` incl. `prospective`, `payment_channel` incl. `unknown`) are reconciled
     with the amendments' names in the Prompt 4 plan, not silently replaced.

### Owner statements of 2026-09-29

Sources: `docs/inputs/OWNER_ANSWERS_ROUND2.md` and the owner's session message of the same
date. Recorded in full in `docs/SCOPE.md` §9 Round 3. They change this ADR as follows.

1. **The correction in item 3 above is resolved.** Questions 1, 2, 3, 5 and 6 are now confirmed
   by the owner in writing. So are 13, 21, 22, 24, 26 and 28. Item 3 of the amendments'
   blocking list (questions 1–3 and 6) is closed.
2. **D1-B stands, with a different enrolment.** Operators never see a Stax hosted page. Staff
   enter the bank method in the Stax dashboard and link the Stax customer ID and
   payment-method ID in the app. The amendments' "invite to autopay" step in Prompt 7a and the
   `stax_schedule_state` value `invited` are dropped.
3. **Operator emails are no longer a go-live blocker.** They are needed only for the receipt
   Stax sends after a payment. Item 2 of the blocking list becomes a receipts gap. Prompt 6's
   rule changes to: an operator without an email is valid **and** eligible for autopay.
4. **No cards.** The per-lease card exception is removed from the model.
5. **Adjustments use one-off invoices.** The app never edits a schedule's amount for an
   adjustment. Each adjustment is its own Stax invoice linked to a `charge_adjustments` row
   with a mandatory note. TODO-VERIFY-012 is replaced by TODO-VERIFY-019.
6. **Manual payments** are `zelle`, `wire` or `other`, with a mandatory note. Cash and check are
   not used by the business.
7. **Prompt 4's schema grows** by `charge_notes`, `charge_adjustments`, `insurance_premiums`
   and `insurance_charges` (ADR-0005), and `stores.open_date` is nullable with an app rule.

**Blocking list after these statements:** the ACH per-transaction limit above $12,581 and any
daily cap (Q25); the payment channel for 453 leases (Q7); the sheet version history check
(Q27); TODO-VERIFY-019; the ACH authorisation point in ADR-0005.

**Open conflict for the Prompt 5 plan.** Collecting a one-off invoice from a stored bank method
needs either a schedule with a single occurrence or `POST /invoice/{id}/pay`. `docs/CLAUDE.md`
Rule 2 forbids the second. TODO-VERIFY-019 must establish which call works. If only the pay
call works, Rule 2 needs an explicit owner decision before any code uses it.

### Consequences

- Prompt 4's plan must cover the amended schema and state which naming it adopts.
- `docs/CLAUDE.md` Rule 2 already lists invoice-schedule create/edit/pause/delete as allowed
  write calls.
- Azure Cache for Redis no longer accepts new instances (observed 2026-09-28). The Redis
  choice for Step 10 (Azure Managed Redis or a Redis container app) is a separate decision to
  be recorded then. Until then `deployRedis=false`.

---

## ADR-0005 — Annual liability insurance is core collection scope

**Date:** 2026-09-29
**Status:** Accepted (owner statement in writing, 2026-09-29)

### Context

`docs/SCOPE.md` treated the sheet's Ins column as a one-time opening balance and left its
collection to Phase 7. The owner stated on 2026-09-29 that liability insurance is an **annual**
charge, collected by ACH in the same way as rent, for most stores as an annual automatic
payment. Staff track paid years in cell notes because the sheet has one cell per store.

### Decision

1. Annual insurance is **MVP scope**, alongside rent. Sign and Deposit/Access stay one-time
   opening balances whose collection remains in Phase 7.
2. The model gains `insurance_premiums` (one amount per policy year, admin-editable) and
   `insurance_charges` (one per **lease** per policy year). Known premiums: 2025 = 595.00,
   2026 = 695.00.
3. Charges follow the algorithm the owner approved in `docs/inputs/OWNER_ANSWERS_ROUND2.md`:
   due on the anniversary of the store's open date, none after closure, status derived from
   payments, a voided or returned payment counts as no payment.
4. **Keyed to the lease, not the store.** The operator holding the lease on a year's
   anniversary owes that year. This supersedes the file's store-keyed wording.
5. Under autopay, insurance is a **separate annual Stax scheduled invoice**, never a line on
   the rent invoice.
6. `stores.open_date` is nullable. A missing date blocks insurance scheduling only.
7. The Payments Balance tab stays out of scope. Only the fact that first-year insurance was
   charged at setup is used, as an opening status.
8. On import, a multi-year Ins value is never split by arithmetic. Its composition comes from
   the cell note and the row is always flagged for review. The file's 1,130 = 595 + 535
   example is withdrawn by the owner.

### Alternatives rejected

- **Insurance as a line on the monthly rent invoice.** Rejected by the owner: the two must be
  paused, refunded and reconciled independently.
- **One insurance balance per store**, as in the sheet. It cannot show which year is unpaid
  and is the reason multi-year sums appear in one cell.

### Consequences

- Prompt 4 builds the two tables. Prompt 5's interface must create an annual schedule.
  Prompt 7's run view lists insurance due in the next 7 days. Prompt 8's return path reopens
  an insurance year exactly as it reopens a rent charge. Prompt 13 parses cell notes.
- Tests must cover: open date Feb 29; open date in the future; closure one day before the
  anniversary; missing premium; payment voided after counting as paid; two payments for one
  year; missing open date; lease transfer between two anniversaries.
- Open: premiums for policy years before 2025 are not stated. Imported years before 2025 carry
  the amount from the cell note or stay flagged.
- **Risk to raise with the business, not decided here.** Bank details now reach staff directly
  from operators and are typed into the Stax dashboard. The hosted page therefore no longer
  captures the operator's ACH debit authorisation. The business must hold a written
  authorisation per operator, covering both the monthly rent debit and the annual insurance
  debit. TODO-VERIFY-006 is rewritten to ask Stax what it requires. The app stores no bank
  details either way (Rule 1).

## ADR-0006 — The database is the queue; no Celery, no Redis

**Date:** 2026-10-02
**Status:** Accepted (owner amendment to Prompt 10, in writing, 2026-10-02)

### Context

ADR-0001 and ADR-0002 named Celery with Redis for background work and scheduling. By
Prompt 10 the only background work is: the Stax calls the web screens used to make
(operator sync, autopay enable/pause/resume, mark-paid, refund, void, and three reads),
the stored-webhook re-fetches, and one nightly reconciliation. Every one of these already
goes through a service function with row locks, creation keys and external-reference
checks, so running it twice changes nothing. The owner's amendment: *no Celery and no
Redis; use the database as the queue; the nightly reconciliation runs as an Azure
Container Apps scheduled Job with a lock; fall back to a Redis container app only if the
database queue proves insufficient, and never Azure Managed Redis.*

### Decision

1. **Table `jobs` is the queue** (`docs/qa/PLAN-010-worker-and-reconciliation.md` D1).
   A web view writes its local change, inserts one row, and redirects. The row holds a
   closed `kind`, the entity, identifiers and amounts as text, a one-time `replay_key`
   from the form, and the outcome sentence. Nothing in it is provider text.
2. **One `worker` process claims and runs jobs** with `SELECT … FOR UPDATE SKIP LOCKED`,
   one at a time, then stored webhook events never attempted. It is the only process
   with `STAX_CALLER=worker`, so the limiter, the breaker and the failure budget have
   one owner (docs/CLAUDE.md Rule 3). It runs as the container app
   `finance-<env>-worker`, one replica, no ingress. The web app runs with
   `STAX_CALLER=none` and scales.
3. **Jobs never retry by themselves.** A failed job stays on the page as "Failed: …"
   with the same single manual retry the screens had, which creates a new job. A job
   whose worker died mid-run is queued again once, then marked `dead`.
4. **The nightly reconciliation is a Container Apps scheduled Job that only queues.**
   Cron is UTC-only, so it fires at 06:00 and 07:00 UTC; `reconcile --scheduled` applies
   the local-time gate (02:00 America/New_York, once per local date) and queues one
   `reconcile_run` job; the worker runs it under a PostgreSQL advisory lock. The job
   container has `STAX_CALLER=none` and never calls Stax. *(First draft ran the
   reconciliation in the job container; rejected in QA because it made a second Stax
   caller with its own limiter.)*
5. **No Redis.** `docker-compose.yml` keeps Redis only until the next housekeeping
   change removes it; nothing reads `REDIS_URL`. If the database queue ever proves
   insufficient (it will not for one worker and a few hundred jobs a day), the fallback
   is a Redis **container app** inside the environment, never Azure Managed Redis.

### Alternatives rejected

- **Celery + Redis** (ADR-0001): a broker, a beat process and a result store for a
  workload of a few hundred idempotent calls a day; two more things to deploy, secure
  and monitor, and the owner declined the cost.
- **Running the reconciliation inside the scheduled Job container:** a second process
  with its own in-memory limiter could send up to twice the design rate against Stax's
  per-IP limit.
- **A thread inside the web process** (PLAN-008's webhook thread): dies with the
  replica, cannot be the single caller once the web app scales, and was removed.

### Consequences

- docs/CLAUDE.md §2 still names Celery + Redis; it is superseded by this ADR for the
  queue and the schedule. Rule 3's "token bucket in Redis" reads as "in the worker
  process, persisted through `sync_state`".
- A staff action now shows "Queued" for up to a few seconds; the page refreshes itself
  every five seconds while a job waits. The screens' outcome sentences are unchanged.
- The owner's done-when for Prompt 10 is met through the worker: an operator edit reaches
  Stax within a minute; the scheduled Job's firing at 02:00 New York is visible as a
  `reconciliation_runs` row with trigger `scheduled`.
- Known gap: the daily summary email (Prompt 10 item 3) waits for the Phase 7 email
  provider; until then the reconciliation page is the daily report and Sentry's fatal
  alert is the notification.

## ADR-0007 — Setup fees are charges with a cost side; a deposit is a liability; year-one insurance stays an insurance charge

**Date:** 2026-10-05
**Status:** Accepted (owner's Prompt 10b text of 2026-10-04; owner confirmed in writing on 2026-10-05: "first_year_insurance stays inactive, as built")

### Context

The business charges each operator one-time setup items at a markup over its own cost, and pays
the landlord a monthly rent the sheet never recorded. The sheet's Ins / Sign / "Deposit or
Access" columns are operator-side balances only. Nothing in the app held a cost, so no margin
could be shown.

### Decision

1. **`fee_types` catalogue, `setup_charges` per lease.** A setup charge has a `price` (what the
   operator owes, collected through the existing payments ledger and, for ACH, a one-off Stax
   invoice linked with the existing link action) and a `cost` (ours; null means unknown). Status
   is derived from settled payments exactly as rent charges are. Nothing new is created on the
   Stax side.
2. **A security deposit is a liability, not revenue.** Refundable fee types are excluded from
   every revenue figure and shown as "Deposits held". At lease end a deposit is resolved once:
   refunded (a `refund_deposit` payment, money out, manual method, never a Stax refund),
   withheld (revenue in the month of resolution) or transferred (a paid deposit on the
   operator's other lease).
3. **Year-one insurance stays an `insurance_charges` row** (ADR-0005) and its cost is
   `insurance_premiums.cost` (300.00 for 2025 and 2026). The `first_year_insurance` fee type is
   seeded **inactive** so a lease is never billed twice for the same year. The owner can activate
   it in the admin if the business prefers the setup line; the insurance algorithm would then
   need a rule to skip year one, which is a new decision.
4. **`store_costs` is date-effective** (landlord rent, other monthly cost, landlord lease end),
   one open row per store, no overlap. A store without a row has an **unknown** cost, never 0.
   The owner's `Store_Costs_Template` is imported by `import_store_costs`, idempotently.
5. **The P&L** (`/reports/pnl/`) shows billed and collected bases side by side, marks unknown
   inputs instead of treating them as zero, highlights months a store costs money with no
   operator, and never counts deposits, voided or returned payments, or waived charges.

### Consequences

- `payments` and `invoices` gain a fourth target (`setup_charge`); `PaymentKind` gains `setup`
  and `refund_deposit`. The webhook transaction handler, the reconciliation, refunds, voids,
  returns and the ledger all know the new target.
- Step 13 imports the sheet's three balance columns as `setup_charges` with `legacy=true` and
  `cost` unknown, then removes `store_balances`.
- New dependency `openpyxl==3.1.5` (MIT), used by the importer only.
- Known limitation: the payments-ledger "Deposits held" total is over the filtered set, not a
  balance at a date; the P&L footer is the balance at the end of its range.

## ADR-0008 — Documents live in private Blob Storage; files are checked for bank data; images are covered by policy

**Date:** 2026-10-05
**Status:** Accepted (owner's Prompt 10c text of 2026-10-05)

### Context

Staff keep lease agreements, amendments, floor plans, photos and notices in shared drives and
inboxes. The app reserved a `documents` table in Prompt 6. A document store is also the easiest
way for bank details to enter a system that must never hold them (docs/CLAUDE.md Rule 1): a
scanned ACH authorisation or a voided cheque is exactly what an operator sends with a lease.

### Decision

1. **Where files live.** One private Azure Blob Storage container per environment, in the
   environment's resource group. Anonymous access is off, shared-key access is off (no account
   key exists to leak), and the app reaches it with its managed identity. A file is never written
   to the container filesystem or to the database; the database holds the row (owner, kind, name,
   type, size, blob path, sha256, who and when).
2. **How files leave.** Only through the app, streamed to an authenticated user whose role may
   read. No public URL and no shared-access link is ever produced. Preview is limited to PDF, PNG
   and JPEG and is served sandboxed.
3. **What may be uploaded.** pdf, docx, jpg/jpeg, png, heic, up to 25 MB, the type decided from
   the content and required to agree with the extension.
4. **Bank-data guard on files.** Text is extracted from every PDF and DOCX at upload and run
   through the same card, routing and account detectors as every text field. A hit refuses the
   file with the same sentence and stores nothing. A PDF that cannot be read (encrypted, corrupt)
   is refused because it cannot be checked.
5. **The stated limit: images.** A photo, a scan or a PDF made of page images cannot be checked.
   The rule for those is policy, shown on every upload box: **no bank forms, ACH authorisations,
   voided cheques or card details; those stay in Stax or DocuSign.** An admin deletes a file that
   breaks the rule. OCR is not used: it would read bank details into this app's memory and logs,
   and a missed digit gives false comfort.
6. **Deletion is soft.** An admin delete sets `deleted_at`; the blob stays. The platform keeps
   deleted and overwritten blobs for 30 days (soft delete and versioning). The database's
   point-in-time restore window is 7 days, so a restored database always finds its blobs.
7. **Audit.** Every upload, refused upload (reason code only), preview, download and delete
   writes an audit row.

### Alternatives rejected

- **Files in PostgreSQL** (bytea): backups and restores carry every file; the owner asked for
  the opposite.
- **Short-lived SAS links for download:** a link can be forwarded for as long as it lives, needs
  a user-delegation key, and puts a storage URL in a browser. Streaming needs neither.
- **Account-key or connection-string access:** a secret to store, rotate and leak.
- **OCR of images:** see point 5.

### Consequences

- docs/CLAUDE.md Rule 1 gains the file policy. `docs/DATA_OWNERSHIP.md` gains the table.
- Known gaps, recorded for the owner: no virus scanning (Microsoft Defender for Storage is the
  option); the storage endpoint is public with Entra-only auth (a subnet restriction is the
  hardening option); the account is locally redundant (GRS is the option for a regional loss).
- A removed file that broke Rule 1 remains in blob soft delete for 30 days; purging it sooner
  is a manual owner action in the portal, recorded when it happens.

## ADR-0009 — One creation path for every non-rent charge; "Charge now" uses pay-an-invoice; cancel deletes the Stax invoice

**Date:** 2026-10-05
**Status:** Accepted (owner's Prompt 10d text of 2026-10-05)

### Context

Every amount that is not monthly rent (old arrears, adjustments, damage, later NSF fees and
installments) could only be created in the Stax dashboard and linked. Rule 2 said "pay an
invoice" (`POST /invoice/{id}/pay`) is not used, pending TODO-VERIFY-019. The owner now wants
these charges created from the app, collected the way staff choose, and, for "Charge now", paid
immediately from the operator's linked bank method.

### Decision

1. **One table, one path.** `setup_charges` becomes `one_off_charges` with a `category`
   (`setup`, `arrears`, `adjustment`, `damage`, `other`) and a `collection`
   (`none`, `autopay_due`, `email`, `manual`, `charge_now`). Setup charges, and later NSF fees
   (11b) and installments (12b), are created through `create_charge`. Payment kind `setup`
   becomes `one_off`. `charge_adjustments` stays for cross-store balance moves until Step 13.
2. **The app creates the Stax one-off invoice** through the worker, local-first (our
   `INV-YYYY-NNNN` row exists before the call and travels as `meta.reference`; the description is
   the single line item; ACH only; check-before-create on every retry; a 4xx is never retried).
   `email` sends it through Stax; `manual` leaves it for a hand-recorded payment; `autopay_due`
   creates a Stax scheduled invoice with **one occurrence** on the due date from the linked bank
   method (TODO-VERIFY-019's working assumption) and Stax generates the invoice that day.
3. **"Charge now" uses `POST /invoice/{id}/pay`** with the linked bank method and a stored
   idempotency ID. Rule 2's prohibition is lifted for exactly this call under these conditions:
   admin only; a confirmation page naming operator, store, amount, invoice number and the bank
   method's last four; the large-debit confirmation above the threshold; **one request, never an
   automatic retry** (a dedicated no-retry policy in the adapter); the same idempotency ID on
   every later deliberate attempt; a re-fetch first so a paid invoice is never charged; behind
   `STAX_PAY_INVOICE_ENABLED`, which stays off on staging until Stax enables ACH. The result is a
   pending ACH payment that settles through webhooks and the reconciliation like any other.
4. **Cancel of an unpaid charge deletes the Stax invoice** (`DELETE /invoice/{id}`; Stax has no
   "void" for an invoice and shows `DELETED`) or the unfired schedule; the local charge becomes
   `voided` with a note only after the provider confirmed. A charge with any payment follows the
   refund path instead.
5. **P&L:** `arrears` counts as rent billed and collected; the other categories as fees;
   refundable fee types stay deposits.

### Alternatives rejected

- **A separate table per kind of charge:** three more copies of payments, invoices, marking and
  reporting logic; the owner asked for one path.
- **Our worker debiting on the due date** for `autopay_due`: the app would become the scheduler
  of money movement; Stax's schedule already is.
- **Retrying a failed "Charge now" automatically:** a retry on an unknown outcome is the one
  thing an idempotency ID cannot fully protect against at a processor with a 10-failures-a-minute
  limit; the owner chose one attempt and a visible failure.

### Consequences

- docs/CLAUDE.md Rule 2 is amended (the "not used" sentence on pay-an-invoice becomes the
  conditions above). `STAX_PAY_INVOICE_ENABLED` remains the switch.
- TODO-VERIFY-019 stays open until the sandbox has ACH: whether `meta.reference` returns on GET,
  whether a one-occurrence schedule behaves, whether a deleted invoice reads `DELETED` or 404.
- The audit log page `/audit/` (admin) exists from this step; the Documents sections show an
  Activity list.

### ADR-0009 addendum (2026-10-06): Waive and a live invoice

Owner finding: waiving a one-off charge whose invoice was live at Stax left the invoice
collectable. Decision: Waive is refused while the provider invoice is live ("Cancel first");
Cancel deletes the invoice and closes the charge; a charge already waived with a live invoice
gets an admin action "Delete provider invoice" (the Cancel path, charge stays waived) and is
listed on the dashboard until cleared. Both actions are audited and explained on the page.

## ADR-0010 — Mail by a delegated service account; NSF only by staff; New York time

**Date:** 2026-10-06
**Status:** Accepted (owner's Prompt 11b text and items 7–8, 2026-10-06)

### Decision

1. **Outbound mail** goes through the Gmail API as the company mailbox, authenticated by a Google
   Cloud service account with domain-wide delegation and the single scope `gmail.send`. No
   password and no SMTP app password exist. The service-account key lives in Key Vault
   (`gmail-service-account-key`) and reaches the worker and the web app as a secret reference;
   no model ever reads it; it is redacted from logs and Sentry; nothing outside `backend/mail/`
   imports Google's libraries. Mail is off by default (`MAIL_ENABLED`). **Outside production a
   catch-all address is mandatory**: every message, whoever it is for, is delivered to the staff
   catch-all with the real recipient named in the subject and a header. The domain's SPF already
   includes Google; the Workspace DKIM key should be published (plan §9).
2. **The collections ladder** (day 3, 10, 20 by default; admin-editable steps and templates with
   whitelisted merge fields) runs once a day from a queue-only scheduled job; the worker sends.
   One message per charge and step; one step per tick; never to an operator handled personally,
   without an e-mail, or for a charge that is paid, waived, voided or pending. Every message is
   logged with its status. The statement link in a message is a signed, 30-day, revocable,
   read-only page with no bank data and no links into the app.
3. **NSF rule.** A returned debit is never charged a fee by the webhook. Staff resolve it in the
   returns queue with **Reschedule**: an agreed re-debit date, one Stax one-off invoice with two
   lines (the original amount and the NSF fee: 100.00 on the operator's first return, 50.00 on
   later ones, both admin-editable fee types, waivable by an admin with a note). Locally a
   `redebit` charge and an `nsf_fee` charge; the money is owed once (the re-debit carries the
   source charge's balance, and a settled re-debit pays the source charge); the counter
   increments on the staff action only.
4. **Time.** The project time zone is America/New_York; storage stays UTC. Dates derived from
   provider timestamps before the switch are realigned only after the owner has seen the list
   (`tz_audit`), by `tz_apply`, each row audited.
5. **Attention list, contact log, calendar and ICS feed** as PLAN-011b D6–D8: shared staff notes;
   a per-user revocable 256-bit feed token, hashed at rest, rate-limited, audited without the
   token.

### Alternatives rejected

- SMTP with an app password (a password to store and rotate); a transactional mail provider (a
  new vendor for a few hundred messages a month; the owner named Workspace); an NSF fee created
  automatically on the return webhook (bank-side errors would be fined); keeping UTC days (the
  books are kept in New York).

### Consequences

- One infrastructure deployment: vault references for the key, mail settings, a daily scheduled
  job. Go-live needs the Workspace admin steps in plan §9.
- Known limits: the two-line Stax invoice and schedule are verified only when ACH works on the
  sandbox; the ICS and the statement link are credentialed by their URL (revocable, logged).

## ADR-0011 — Contact log maturity; one invoice builder for every non-rent bill; disputes read-only with evidence

**Date:** 2026-10-07
**Status:** Accepted (owner's Prompt 12 text and amendments, 2026-10-07). **S1 and S2
accepted by the owner on 2026-10-07 and built in PLAN-012a (below).**

### Decision

1. **Contact log (owner findings (1)–(4) of 2026-10-06).** An entry is the author's to edit for
   15 minutes; afterwards only an appended correction changes what it says; an admin may soft
   delete it with a reason (struck through for billing and admin, invisible to viewers); nothing
   removes a row. Entries name the store (required with more than one active lease). A promise
   closes by itself as **kept** when a settled, non-refund payment of at least the promised
   amount settles on that lease (or operator) between the recording day and the promised date;
   recording a new entry while a promise is open asks for the previous promise's outcome:
   **Kept, Rescheduled** (a new promise) or **Broken**. Statuses `open`, `kept`, `rescheduled`,
   `broken`; old closed promises were migrated as `broken` (accepted 2026-10-07).
2. **Invoice builder.** "Create one-off charge" is removed (its addresses answer 410). Every
   non-rent bill is a draft with lines (fee type or free text, quantity, unit amount), previewed
   and printed, then **sent as one Stax invoice with all its lines** or charged now (admin).
   Each line becomes one one-off charge (ADR-0009 unchanged), each line item names its charge
   (`invoice_line_items.one_off_charge`), and a payment on the invoice is allocated to the
   lines in order; the returned-then-rescheduled rule and deposits held read the lines. The
   builder offers no autopay-on-due-date collection; an ended lease may still be billed by
   e-mail (arrears).
3. **Disputes.** Stax owns dispute state. The app keeps a read-only list (`disputes`), read by
   the worker from `GET /underwriting/disputes` (daily, on a dispute webhook, on Refresh), with
   status and reason verbatim, the transaction masked, the payment, operator and store derived
   from our refs. The one write is **evidence upload** (`POST /file/dispute`, multipart): an
   admin's action on a file stored first as the operator's document through the 10c path (the
   Rule 1 checks unchanged; TIFF accepted for this kind only; images need the notice ticked),
   queued to the worker, **not retried** after a 5xx or timeout (not idempotent), refused once
   `respond_by` has passed; a provider refusal is shown as its sentence, never a traceback.
   Nothing accepts, contests or closes a dispute here. Rule 2 of `docs/CLAUDE.md` is amended
   accordingly; the full workflow is BACKLOG §1 row 4.
4. **Freeze.** Nothing in `docs/BACKLOG.md` §1 starts before Step 17 (Checkpoint 12).

### Schema recommendations S1 and S2 (owner's answers, 2026-10-07)

- **S1 — line-item kind.** `invoice_line_items.kind` (`charge`, `fee`, `surcharge`, `tax`,
  `discount`) with `invoices.fees` and `invoices.surcharge` as separate stored columns
  (default 0). Recommended now (the builder is the first code that writes several lines per
  invoice). **Decision: yes (owner, in writing, 2026-10-07). Built in PLAN-012a (QA-012a):**
  `invoice_line_items.kind` (CHECK), `invoices.fees` and `invoices.surcharge` (default 0,
  never negative, `total` is the sum of the parts); the builder writes `charge` lines; the
  provider contract is unchanged and the two amounts are never sent.
- **S2 — operator contact consent.** `operators.sms_phone` (E.164), `sms_consent_at`,
  `email_consent_at`. Recommended before the Step 13 import. **Decision: yes (owner, in
  writing, 2026-10-07). Built in PLAN-012a:** `operators.sms_phone` (E.164, CHECK and
  guard), `sms_consent_at`, `email_consent_at`, edited by billing on the operator form; the
  Step 13 import maps the sheet's phone column where present; no channel uses the columns
  until BACKLOG §1 item 1.

### Consequences

- Two new Stax calls, both through the worker, the limiter and the breaker; the upload is a
  staff action, the list a paged daily read (BACKLOG §1 row 4 risk column).
- `documents` gains an operator owner (dispute evidence only, CHECK); `disputes`,
  `dispute_evidence`, `invoice_drafts`, `invoice_draft_lines`, `contact_corrections` are new
  tables; `contact_entries` gains `lease`, `status`, `kept_by_payment` and the soft-delete
  columns; job kinds `disputes_sync` and `dispute_evidence_upload`.
- TODO-VERIFY-062 (upload response shape) and 063 (wording of the late-upload 400) are open
  until the first upload on sandbox or live.

## ADR-0012 — Deposits at lease end; proration; installments count once

**Date:** 2026-10-13
**Status:** Accepted (owner's Step 12b rules, confirmed in writing 2026-10-13; the owner's
decisions of 2026-10-14: single rounding kept, `reportlab` accepted, debt after a transfer
blocks unless an admin overrides with a reason, follow-up 12b-a approved)

### Decision

1. **Deposit at lease end.** A security deposit stays a liability until the lease ends (ADR-0007).
   The end-of-lease wizard settles it in one resolution row: the part refunded (by Zelle, wire or
   check, recorded as a `refund_deposit` payment, never through Stax) and the part withheld
   (deductions typed as lines: damage, cleaning, unpaid-balance offset, other). An unpaid-balance
   offset moves the withheld amount onto the open charges, oldest first, as settled manual
   payments of method `other` with the reference "deposit offset", so the receivable reads paid
   and the liability released; what the deposit does not cover becomes one charge, "Lease-end
   balance", collected by hand. The refund is a liability release, never an expense; a withheld
   amount is revenue in its month, as ADR-0007 says. The final statement is stored as a PDF under
   the store's documents (kind `final_statement`) and e-mailed to the operator when mail is on.
2. **Proration.** A lease charged mid-month pays for the days used: amount = monthly rent × days
   used ÷ days in that calendar month, **rounded once, at the end, to the cent** (the daily rate
   itself is never rounded; the owner's example: 13–31 Oct, 19 of 31 days of $2,000.00 =
   $1,225.81); start and end days both count; "Prorate first month" on the lease (default on)
   and "Prorate last month" in the wizard (default on). The rule's wording is this line
   (owner, 2026-10-14).
3. **Transfer.** The outgoing lease is ended through the same wizard (reason `transfer`); the
   incoming lease starts no earlier than the day after; the incoming operator's deposit and setup
   charges are a builder draft for review, so nothing reaches Stax before staff send it; no day
   has two leases in effect on one store. **Debt after a transfer** (owner, 2026-10-14): when the
   statement would leave a "Lease-end balance" on the outgoing operator, the transfer is blocked
   until an admin allows it with a written reason; the override is audited, the charge stays on
   the outgoing operator and sits on the attention list until paid.
4. **Installments.** A plan splits an open charge into 2–12 charges of category `installment`
   (equal amounts, the rounding cent on the last), collected as the parent is; the parent is
   covered by the plan and counted once: billed as itself, collected through its installments;
   a returned installment follows the NSF rule and puts the plan on the attention list;
   reschedule shifts the unpaid ones, cancel returns the remainder to the parent. Each
   installment payment is counted in the month it settled, like every other payment (a split
   never rewrites a closed month). A live plan on a charge of a lease blocks that lease's end
   until it completes or is cancelled (added 2026-10-14 from the QA gate).
5. **One new dependency**, `reportlab` (BSD), to write the statement PDF; the alternative (the
   browser's print, no stored document) does not meet "stored under the store's documents".
   Accepted by the owner 2026-10-14.
6. **Follow-up 12b-a** (owner, 2026-10-14): splitting a charge that has a live Stax invoice or
   schedule deletes the parent's Stax object without voiding the charge and recreates the
   collection per installment; built right after the 12b push, before Step 12d. Extended to
   rent charges by PLAN-012b-b D13 (owner's item 9, 2026-10-15): a rent invoice is released the
   same way, and a rent autopay schedule is moved past the split month (deleted and created
   again from the next month's debit).
7. **The P&L at lease end** (owner's item 8, 2026-10-15; PLAN-012b-b D12). A deposit charge is
   never a fee billed and a deposit payment never a fee collected. At lease end, the deductions
   other than the unpaid-balance offset are the **fees billed of the end month**: the part the
   deposit covers (the withheld amount less the offset) is billed and collected in that month
   (shown apart as "withheld deposit"), and the rest is the "Lease-end balance" charge, billed in
   that month and collected when it is paid. The offset part is neither billed nor collected at
   the end (it paid existing charges, counted where those were billed). So for any month billed
   is at least collected, and the deposits-held line falls by the withheld and refunded parts.
   Example (Test Store Three): deposit $500 paid, damage $800 → October fees billed $800, fees
   collected $500 (withheld), lease-end balance $300 open, deposits held $0.

### Consequences

- New tables `lease_ends`, `lease_end_deductions`, `lease_transfers`, `installment_plans`; new
  columns on `leases`, `rent_charges`, `one_off_charges`, `outbound_messages`; mail gains
  attachments; migrations `ledger` 0034–0038 and `payments` 0016–0017; `docs/CLAUDE.md` §5 ("send email itself") is amended by the
  final-statement e-mail, which goes through the same mail package and catch-all as the ladder.

## ADR-0013 — Manual payment allocation and lease credits

**Date:** 2026-10-15
**Status:** Accepted (owner's Step 12d text, 2026-10-15; built by PLAN-012d Batch B; confirmed by
the owner in writing 2026-10-16)

### Decision

1. **A payment is allocated by hand.** When staff record a payment from the store page they
   enter the amount, method, date and note once, and then type how much of it goes to each open
   charge of the store's current lease (rent, one-off, insurance), oldest due first. Nothing is
   pre-filled unless exactly one charge is open. The form refuses to save until the allocations
   add up to the payment, or the user keeps the remainder as a credit. Each allocated part is a
   settled payment on its charge, written through the existing per-charge rules (one payment
   row per charge, the same Stax paid-as-cash marking once a charge's invoice is fully covered,
   partial coverage flagged for review), all parts sharing one allocation group and one
   idempotency key family.
2. **The remainder is a credit on the lease, not revenue.** Money received and not applied is
   a liability of the business to the operator, like a deposit: it is never billed, never
   collected in the P&L, shown as "Credit $X" in the store header and as "Credits held" in the
   P&L footer. Applying a credit to a charge later is a settled payment of method `credit` (no
   provider involved); it counts as collected in the month applied, under the charge's
   category. A credit is refunded only through the lease end (a refund-due line settled by the
   settlement method, like an overpaid last month); a transfer is refused while a credit
   remains. Receivables ignore credits.
3. **Why manual.** Automatic allocation (oldest first) would hide the operator's intent (a
   payment meant for the deposit, not the rent) and make the Stax marking unpredictable; the
   spreadsheet practice was to write the money against the row the operator paid for.

### Consequences

New columns `payments.allocation_group`, `payments.credit_id`, a new table `lease_credits`, a
new payment method `credit`; the lease-end statement and the transfer blockers read credits;
the per-charge payment forms stay for single-charge entry from a charge page.

## ADR-0014 — Repeating debits as a local plan of single-occurrence schedules

**Date:** 2026-10-24
**Status:** Accepted (owner, 2026-10-24: one line per plan, the seven-day lead and the large-debit
confirmation once at setup, audited per occurrence, confirmed as PLAN-012f-a §9 items 6–8; built by
Batch E under the same assumptions)

### Context

The owner asked for a sixth "How to collect" choice, "Set up autopay": repeat an invoice as bank
debits monthly, weekly or yearly from a first date, ending after N payments, on a date or never,
with Pause / Resume / Stop and an editable end rule on the store's Autopay tab, each run creating
that period's charge and Stax schedule like a scheduled one-off, rent excluded (owner, 2026-10-23).
What the code had: "Schedule for a date" creates one `autopay_due` one-off charge and a
single-occurrence Stax schedule (`DTSTART…;FREQ=DAILY;COUNT=1`) at once; a repeating Stax schedule
cannot back one charge (adoption requires `COUNT=1`, a second invoice on a charge is refused); the
catalogue's unique (lease, fee type) refuses a repeated catalogue item; `CollectionSchedule` holds
monthly rent and yearly insurance only; weekly and yearly Stax rules are unverified
(TODO-VERIFY-019/036); no daily job created one-off charges.

### Decision

1. **A plan is local; Stax sees one single-occurrence schedule per debit.** `AutopayPlan` holds the
   line (one item, quantity, unit amount, amount), the cadence, the first date, the end rule, the
   state and `next_on`. Each occurrence is one `OneOffCharge` (category `autopay_plan`, collection
   `autopay_due`, the plan and a period number) created through the existing one-off path, which
   queues the existing `schedule_create` job; the rule sent stays `FREQ=DAILY;COUNT=1`. No repeating
   rule is ever sent, so nothing unverified at the provider is relied on.
2. **The daily run creates the next occurrence seven days before its due date** (`LEAD_DAYS`), the
   first at setup. The Stax debit hour is 12:00 UTC on the due date and the tick runs at 09:00 New
   York, so the occurrence must exist before its day; a week gives staff time to see it. A missed
   run catches up: the occurrence falls due today and is collected late, never skipped (at most
   twelve a run). The run is idempotent (a creation key per plan and period, the plan locked).
3. **Rent is excluded; one line per plan.** A rent line is refused as the Schedule choice refuses it
   (rent stays on the lease's autopay). The plan takes the invoice's one line; several lines would
   need a multi-line schedule linked to several charges, which the schedule path cannot do today.
4. **Pause and Stop cancel the pending occurrence** (the existing cancel, which deletes its Stax
   schedule while no invoice was made) and set the state; Resume makes the next occurrence due from
   today; Stop is final; Change end rewrites the rule and finishes a met plan at once. Billing may
   pause and resume; admin stops and changes the end. Each is audited; the History tab lists them.
5. **The large-debit confirmation is given once at setup** and stored on the plan; every occurrence
   above the warning amount carries the audit row naming that confirmation; a plan above the
   provider limit is refused at setup. The daily run has no human to ask per debit.
6. **A live plan blocks a lease end or a transfer** like a one-off schedule does, until stopped.

### Alternatives rejected

One repeating Stax schedule per plan (cannot be linked to per-period charges; weekly and yearly
rules unverified; a pause would be a provider-side edit of an amount-bearing schedule, which
ADR-0009's rule against editing schedule amounts forbids); a new `CollectionSchedule` kind (its
debit day, seq and uniqueness cannot hold weekly or yearly plans; it is the rent and insurance
contract); creating the whole series of charges at setup (months of open charges on the books,
each a Stax schedule, before they are due).

### Consequences

New table `autopay_plans`, two columns on `one_off_charges`, one category value, one migration
(0049) that refuses to reverse while plans exist; the daily run gains a step; the plan is the
source of truth for the cadence and the end rule, Stax for each debit's state; pausing deletes the
pending debit's schedule (the only Stax write besides creation); a period number is internal and
can exceed the count after a pause, so the charge's description carries the made ordinal.

---

## ADR-0015 — An operator is a person; billing details, with the processor customer, belong to each lease

**Date:** 2026-10-30
**Status:** Accepted (owner, 2026-10-29: "in Stax every customer is a store. We keep operators
separate only to see all stores of one owner together and to follow their total debt"; built by
PLAN-014 Batch C)
**Deciders:** Project owner; plan and record by Fable; built by Opus

### Context

PLAN-006 made the **operator** the processor's customer: one Stax customer per operator, its
bank method linked to the operator, invoices and schedules for every store of the operator
raised against that one customer, receipts to the operator's e-mail (`docs/SCOPE.md` §4 "One
Stax customer per operator"; ADR-0004 item 2 for the staff-entered method). The owner's
business runs the other way round: in Stax every **store** is a customer with its own billing
identity — company name, e-mail, phone, address and bank account — and an owner with several
stores may pay each from a different account or the same one. The operator record exists to
see one person's stores and their total debt together.

What the code had: `Operator` with a kind (company or person), company, contact person, legal
name, first and last name, e-mail (unique), phone, address and the processor sync columns;
`ExternalRef(entity_type="operator")` rows for the customer and the bank method, the method's
last four in a snapshot on the customer ref; every billing path resolving through
`leases.customer_ref(operator)` and `linked_payment_method(operator)` (31 call sites); no
billing columns on `Lease`; no way for the app to create a payment method (Rule 1).

### Decision

1. **An operator is a person.** The form holds Full name (required, stored in `display_name`),
   Address, Phone and Email. Type, Company name, Contact person and Legal name leave the form
   and every page; their columns stay (history) and are no longer written. The operator's
   e-mail stays unique: it is the person's own address and, from Batch H, the portal login.
2. **Billing details belong to each lease.** `LeaseBilling` (one row per lease): company name
   (optional), e-mail, phone, address, the cached bank last four, and `shares_account_of`.
   Invoices, statements ("Bill to": the lease's company with the operator's full name under
   it), autopay, the Add payment bank-debit card and every message use the lease's details.
3. **Each lease is one processor customer.** The customer and payment-method references are
   `ExternalRef(entity_type="lease")`; the sync state lives on the lease; `customer_input`
   sends the lease's company, the operator's name, the lease's e-mail and phone and the store
   number as the reference. Webhooks map a customer to a lease.
4. **"Use the same account as #1991" shares the processor customer.** A bank method belongs to
   one processor customer and cannot be copied, so a lease that uses another lease's account
   points at it (`shares_account_of`, one hop, no cycle) and bills through that lease's customer
   and method. A lease moves to its own customer when its own customer holds a usable bank
   method and staff press "Switch to own account" (future schedules move through the existing
   `move_autopay`).
5. **The migration copies once and never calls the processor.** Ledger 0059 creates a
   `LeaseBilling` per lease from the operator's details, re-points each operator's live
   customer and method references to its **primary lease** (the active lease with the earliest
   start, then the lowest store number; the external ids do not change, so the processor sees
   nothing), and makes every other lease of that operator share the primary's account, flagged
   "Own processor customer needed". An operator with references but no active lease keeps them,
   flagged. One audit row per lease. Backward: reverses when one lease held the references,
   refuses otherwise.
6. **Per-lease customers are created by a command, on the owner's yes.** `create_lease_customers
   [--dry-run]` creates one processor customer per sharing lease through the lease sync job; it
   never creates a bank method (the app cannot; a method arrives from the Stax dashboard or,
   from Batch H, the operator's own entry through the processor's hosted fields). Its run on
   staging against the sandbox is a Section 6 Publish Request; the live run is TODO-VERIFY-067.
7. **Bank entry through the processor's hosted fields** is part of this model (C7) and is
   decided in ADR-0017 together with the portal: the processor's iframe runs in the browser,
   the app receives a payment-method id and verifies it with the existing read before linking.

### Alternatives rejected

- **One customer per operator with per-store labels on invoices.** The processor's receipts,
  schedules and payment methods cannot be split per store; the owner's practice is per store.
- **Copying the bank token to a new customer per lease.** Not possible: a method belongs to one
  customer; re-entry is the only way, hence the shared account as the bridge.
- **Moving every lease to its own customer in the migration.** It would strip working autopay
  schedules of their bank method; the staged switch keeps collections running.

### Consequences

- `docs/SCOPE.md` §4 ("One Stax customer per operator", caveat 1) and `docs/CLAUDE.md` Rule 1's
  second bullet are rewritten; `docs/qa/PLAN-006-operators.md` stays as history.
- After 0059 on staging, multi-lease operators share until the command and the switch; the
  checkpoint reports the dry run's counts.
- The operator statement spans leases and prints the full name with each store's company; the
  signed statement link stays per operator.
- Messages (Batch D) address the lease's billing e-mail; a lease without one is held.


---

## ADR-0016 — The messaging engine, the reminder rules and automatic re-debits of R01/R09 returns

**Date:** 2026-10-30
**Status:** Accepted (owner, 2026-10-29: Step 14 Batches D, E and F as written in the prompt; built by
PLAN-014)
**Deciders:** Project owner; plan and record by Fable; built by Opus

### Context

ADR-0010 chose mail through a delegated Google Workspace service account (scope `gmail.send`), a
collections ladder of admin-editable steps (day 3, 10, 20) with whitelisted merge fields, and "NSF
only by staff": a returned debit was re-debited only when staff pressed Debit again, which also
added the fee. What the code had: `OutboundMessage` as a log keyed by step (the body kept as a
hash only), `CatchAllSender` for every non-production environment, no send window, no allowlist,
no automatic re-debit, no SMS. The owner asked for one engine for every operator message (e-mail
now, SMS later), Outbox mode until the Workspace key exists, a sandbox allowlist, templates with a
closed placeholder set, rules A–E as a daily job, template F at once on a returned debit, the
NSF fee as its own charge, and an automatic re-debit of R01/R09 returns within Nacha's limits.

### Decision

1. **One engine** (`ledger/services/messaging.py`): every operator-facing or internal message is
   an `OutboundMessage` with a lease, a rule, an occurrence, its rendered subject and body, a
   status (`queued`, `sent`, `held`, `failed`, `suppressed`), a held reason and a scheduled time;
   one row per lease per charge per rule per occurrence (a unique constraint), so a re-run never
   duplicates. A channel abstraction carries e-mail now and SMS later; `sms` is refused until a
   provider exists.
2. **Outbox mode and the allowlist.** With no key, `MAIL_ENABLED` false or no sender address, a
   message is rendered, stored and `held` ("not sent (mail not set up)"); on the sandbox
   environment only addresses on the company's allowlist leave the system, every other message is
   `held` ("held on sandbox"). The catch-all of ADR-0010 is retired. A held message can be sent
   later from Admin › Outbox (`/admin-settings/outbox/`, admin only; "Send now" is refused while mail is not set up or, on the sandbox, while the address is off the allowlist).
3. **The sender is a company setting.** `CompanySettings` holds the sender address, the reply-to,
   the manager list (rule E) and the sandbox allowlist; the Gmail key stays in Key Vault
   (`gmail-service-account-key`). `GMAIL_SENDER` and `MAIL_CATCH_ALL` are no longer settings.
4. **Templates are data with a closed vocabulary.** Admins edit the templates in Admin › Settings;
   the placeholders are exactly the fifteen of the plan; an unknown one is refused on save; the
   body is rendered by the whitelisted formatter, never a Django template; the six templates of
   the owner's prompt are seeded byte for byte, plus F's no-retry variant (plan §8); the three
   ladder templates stay inactive for their messages' history; the ladder's `CollectionStep` is gone.
5. **The send window** is 9:00–18:00 America/New_York: a message queued outside it is scheduled
   for the next 9:00 through `Job.run_after`, which the worker honours.
6. **The rules** (Batch E): rent with an active autopay gets A three days before the debit and
   nothing else while debits succeed; every other open charge gets B three days before due, C on
   day 5, D on day 10 and every ten days from day 40, E once to the managers on day 30; a rule
   fires only on its day (no catch-up); an open promise pauses B–E until its date; paid, waived
   and voided charges get nothing; installments are charges of their own. No late fee.
7. **Returned debits** (Batch F): template F at once with the return code in plain words; the
   $100.00 NSF fee as its own charge, once per returned payment; for R01 and R09 only, an
   automatic re-debit of the original amount on the third business day (Federal Reserve
   holidays excluded), at most two per original within 180 days, the processor schedule created
   only on the eve so a stop is local; staff "Stop automatic retry" or a logged staff contact
   within two business days stops it; every other code gets no retry, puts the lease in Needs
   review and sends F's no-retry variant; a staff void never messages the operator. This
   supersedes ADR-0010's "NSF only by staff" for the fee and for R01/R09 retries; staff keep
   Debit again for the rest.

### Alternatives rejected

- A third-party mail API (ADR-0010's rejection stands).
- Keeping the hash-only log: the Outbox and the timeline must show what was or would be sent.
- Catch-up of missed rules at the first run or after a pause: a flood on day one; the owner's
  text names days, not backlogs.
- Creating the retry's processor schedule at the return: a stop would then need a provider
  delete; the eve-of-retry creation keeps the stop local.

### Consequences

- The body of every message is stored; the recipient is masked below admin; the payment-data
  validator covers the stored text.
- `docs/CLAUDE.md`'s mail notes and `docs/SCOPE.md` §3's collection notes point here; the
  RUNBOOK gains the Outbox and the allowlist.
- Live mail waits for the owner's Workspace delegation (TODO-VERIFY-070); the re-presentment
  limit and the holiday table are TODO-VERIFY-072 and 073.

---

## ADR-0017 — An operator portal at `/portal/` with its own principal; bank accounts through the processor's hosted fields

**Date:** 2026-10-31
**Status:** Accepted (owner, 2026-10-29: Step 14 Batch H as written in the prompt; built by
PLAN-014 H; the hosted-fields premise is TODO-VERIFY-066 until the processor confirms it; the
web-payments token reaches operators' browsers on the bank page by that design)
**Deciders:** Project owner; plan and record by Fable; built by Opus

### Context

ADR-0004 item 2 and Rule 1 of `docs/CLAUDE.md` said that customers never sign in to this app and
that no bank or card detail is ever entered here: bank methods were entered by staff in the
processor's dashboard and linked by id. The owner's statement of 2026-09-29 kept it so for the
first steps. On 2026-10-29 the owner asked for an operator portal: each operator signs in, sees
all their stores, their charges, invoices, payments and statement, pays by bank debit and adds or
replaces a bank account — without the staff app's navigation, roles or MFA, and without a
second identity system entangled with the staff one. The processor's hosted fields (PLAN-014 C7)
keep the account and routing numbers inside the processor's own iframe: the page never receives
them, so Rule 1's substance holds while its old wording ("do not embed Stax.js") does not.

### Decision

1. **A separate principal.** `portal.PortalUser` (an operator's e-mail and a password through
   Django's hashers; not `AbstractBaseUser`, not the staff `AUTH_USER_MODEL`), invited by staff
   from the operator page, with invitation and reset tokens stored hashed (7 days and 1 hour),
   a per-e-mail and per-IP lockout (5 failures in 15 minutes, the same sentence for unknown
   e-mails), and its own session cookie `portal_sessionid` scoped to `/portal/` (HttpOnly, Secure
   outside local, SameSite Strict; 30 minutes idle, 12 hours absolute, rotated at login). The
   staff middleware chain exempts the `portal:` namespace; the portal's own middleware requires
   a signed-in portal user for every portal route but the five public ones. The registry pins
   the staff public set (five names), the portal set and the portal public set.
2. **Isolation by construction.** Every portal query goes through `portal/services.py`, whose
   functions take the portal user and filter by its operator; a foreign id is a 404, never a
   403. A generated test matrix (Fable's) opens every parametrised portal route of operator A
   with operator B's ids.
3. **What the operator sees and does.** All their stores (ended leases marked), each store's
   open charges, invoices, payments and the statement PDF; theme and password; **Pay** by bank
   debit for a charge or a store's balance (a `PortalPayment` and a worker-only `PORTAL_PAY`
   job through the existing pay-an-invoice path, one attempt, the processor limit, behind the
   pay-invoice flag; Rule 2 gains "a portal user's own payment" as the second allowed caller);
   **Bank account** add or replace through C7's hosted fields on the lease's processor customer
   (the superseded method is not deleted at the processor: TODO-VERIFY-069). Autopay is read
   only ("set up by our staff"). A lease sharing another's account sees that store's last four.
4. **Messages.** The invitation and the reset go through the messaging engine (ADR-0016) on
   seeded templates with one extra placeholder each, to the operator's e-mail; the one-time link
   is issued when the e-mail leaves and never stored (a stored link in the Outbox would have let
   staff take over an account); `{pay_link}` in
   the reminder templates resolves to the portal's pay page once the operator has a portal user.
5. **Staging.** The sandbox cannot tokenize a bank account and ACH is off there: the pay page
   says "Online payment is not available yet" and the bank page "not set up"; the browser tests
   run on the fake provider (TODO-VERIFY-068 live ACH from the portal; TODO-VERIFY-066 the
   hosted fields' iframe).

### Alternatives rejected

- The processor's hosted invoice page (a link per invoice): the owner wants the operator
  inside our portal with all their stores, statements and bank account in one place.
- A staff-only bank entry (the previous rule): kept as the fallback path (the Link form) but no
  longer the only one.
- Reusing the staff `User` with a role "operator": one identity system for two audiences would
  entangle MFA, lockout, sessions and the role registry; a foreign-key principal keeps the staff
  chain untouched.

### Consequences

- `docs/CLAUDE.md` Rule 1 reads: no account or routing number is ever typed into, posted to or
  stored by this app; the processor's hosted fields alone collect them. "Customers never log in
  here" becomes "Staff sign in at `/`; operators sign in at `/portal/` with a separate principal."
  Rule 2 names the portal payment as the second allowed caller of pay-an-invoice. `docs/SCOPE.md`
  §2 and §4 name the portal.
- A second login surface: its lockout, tokens and sessions are tested in isolation; the staff
  public set is pinned so the portal can never widen it.
- The Nacha authorisation text on the bank-entry page is the processor's (TODO-VERIFY-006
  stays open).

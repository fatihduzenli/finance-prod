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
**Status:** Accepted (follows from Decision D1, owner-confirmed 2026-09-27; amendments supplied by the owner in `docs/inputs/PROMPT_SERIES_AMENDMENTS.md`)

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

### Consequences

- Prompt 4's plan must cover the amended schema and state which naming it adopts.
- `docs/CLAUDE.md` Rule 2 already lists invoice-schedule create/edit/pause/delete as allowed
  write calls.
- Azure Cache for Redis no longer accepts new instances (observed 2026-09-28). The Redis
  choice for Step 10 (Azure Managed Redis or a Redis container app) is a separate decision to
  be recorded then. Until then `deployRedis=false`.

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

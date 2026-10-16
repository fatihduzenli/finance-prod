# Invoicing app (Stax Pay)

Staff-only internal tool that replaces a Google Sheets invoicing workflow and
integrates with Stax Pay's Merchant API. Customers pay on Stax-hosted invoice
pages; this app never handles card or bank data.

## Read first

- [`CLAUDE.md`](CLAUDE.md) — process rules: model roles, QA gates, secrets,
  publish stop gate.
- [`docs/CLAUDE.md`](docs/CLAUDE.md) — project rules: the eight ground rules
  for working with Stax, environment variables, verified facts.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — architecture decisions.
- [`docs/TODO-VERIFY.md`](docs/TODO-VERIFY.md) — open questions about the
  Stax spec that code must not assume answers to.

## Layout

| Path | Purpose |
|---|---|
| `backend/` | Django project: `finance/` (settings, env guard, banner middleware, Sentry, urls), `accounts/` (sign-in, MFA, roles, sessions), `ledger/` (business tables: stores, operators, leases, charges, invoices, payments; card-number guard; `seed_demo`), `payments/` (provider-side tables: `external_refs`, snapshots, deposits, webhook events, sync state; the `provider_write()` sync writer), `tests/` |
| `infra/` | Azure Bicep templates and the owner's deployment runbook ([`infra/README.md`](infra/README.md)) |
| `scripts/` | Operational scripts, including the `check-no-secrets.sh` build gate |
| `docs/` | Rules, decisions, QA plans and reports (`docs/qa/`) |
| `Dockerfile`, `.dockerignore` | Delivery image: uv build stage, non-root gunicorn runtime |
| `.github/workflows/ci.yml` | Lint, types, unit and e2e tests on every push; staging deploy from `main` |
| `docker-compose.yml` | Local PostgreSQL (and a Redis service nothing reads; ADR-0006) |
| `.env.example` | Every environment variable the app reads, with empty values |

## Stack

Python 3.12, Django 5.2 LTS, PostgreSQL 16, a database job queue with one worker
process (no Celery, no Redis; ADR-0006),
`uv` for dependency management, ruff, mypy in strict mode, pytest and
Playwright. Hosted on Azure Container Apps. See ADR-0001, ADR-0002 and ADR-0006 in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

## Commands

All run from the repository root.

| Purpose | Command |
|---|---|
| Install | `uv sync --all-groups` |
| Lint | `uv run ruff check .` |
| Format check | `uv run ruff format --check .` |
| Types | `uv run mypy` |
| Unit tests | `uv run pytest -m "not e2e"` |
| E2E tests | `uv run pytest -m e2e` (once: `uv run playwright install chromium`) |
| Dev server | `uv run python backend/manage.py runserver` |
| Apply migrations (image start) | `uv run python backend/manage.py deploy_migrate` |
| Apply migrations (local) | `uv run python backend/manage.py migrate` |
| Seed demo data (`APP_ENV=local` only, safe to repeat) | `uv run python backend/manage.py seed_demo` |
| Stax authentication check (one `GET /self`; needs `STAX_CALLER=worker`) | `uv run python backend/manage.py provider_ping` |
| Boot check: key environment and merchant (one `GET /self`; runs in the image `CMD`) | `uv run python backend/manage.py provider_environment_check` |
| Create the annual insurance charges in scope today (idempotent; prints counts; no Stax call) | `uv run python backend/manage.py generate_insurance_charges` |
| Worker: runs queued Stax jobs and stored webhook events, one at a time (the single Stax caller; refuses to start unless `STAX_CALLER=worker`; SIGTERM finishes the current item; `--once` runs one tick) | `STAX_CALLER=worker uv run python backend/manage.py worker [--once]` |
| Process stored webhook events not yet processed (manual catch-up; retries failed events, which the worker does not; prints counts) | `uv run python backend/manage.py process_webhooks [--limit 100]` |
| Queue the nightly reconciliation when it is due (the scheduled Container Apps job runs this; no Stax call; `STAX_CALLER` may be `none`) | `uv run python backend/manage.py reconcile --scheduled` |
| Run a reconciliation at once inside the worker (prints counts; exit 1 on failure) | `STAX_CALLER=worker uv run python backend/manage.py reconcile` |
| Test fixture: age a pending payment so the next reconciliation flags it (refused in production) | `uv run python backend/manage.py age_pending_payment --payment <id> --days 10` |
| Import landlord rents and other store costs from the owner's template (`.csv` or `.xlsx`, sheet "Store costs"); idempotent; `--dry-run` writes nothing; exit 1 when any row was refused | `uv run python backend/manage.py import_store_costs <file> [--dry-run]` |
| Register the app's webhooks with Stax (creates only missing ones; stores the IDs; prints event names and masked IDs, never the URL; refused when `APP_ENV=local`) | `uv run python backend/manage.py register_webhooks` |
| Delete the app's webhook registrations at Stax by stored ID (refused when `APP_ENV=local`) | `uv run python backend/manage.py unregister_webhooks` |
| Dev server without Stax (in-memory provider; `APP_ENV=local` only) | `STAX_PROVIDER=fake uv run python backend/manage.py runserver` |
| Stax sandbox suites (off by default; need `STAX_SANDBOX_TESTS=1`) | `uv run pytest -m sandbox` |
| Secret scan | `scripts/check-no-secrets.sh` |

## Environments and deployment

Three environments, never able to reach each other's data or keys
(`docs/CLAUDE.md` Rule 6). Sandbox and live share the Stax host
`https://apiprod.fattlabs.com`; only the key differs, which is why the
environment guard and the yellow SANDBOX banner exist.

| | Local | Staging | Production |
|---|---|---|---|
| `APP_ENV` | `local` | `staging` | `production` |
| `STAX_ENV` | `sandbox` | `sandbox` | `sandbox` for now — `live` at Step 16 |
| Stax key | sandbox key, or empty | `stax-api-key-sandbox` | `stax-api-key-sandbox` now, `stax-api-key-live` at Step 16 |
| `STAX_CALLER` | `none` (default) on the dev server; `worker` for the `worker` command and `provider_ping` | web app `none` (up to 2 replicas); worker app `worker` (one replica, PLAN-010 D4) | web app `none`; worker app `worker` |
| `STAX_PROVIDER` | `stax` (default), or `fake` for the in-memory provider | `stax` | `stax` |
| `STAX_PAY_INVOICE_ENABLED` | `false` (default) | `false`; `true` only for the documented sandbox test | `false` until TODO-VERIFY-019 is decided |
| `LARGE_DEBIT_WARNING_AMOUNT` | empty (`2000.00`) | empty (`2000.00`) | empty (`2000.00`); a placeholder until Stax states the per-transaction limit (Q25) |
| `STAX_MAX_TXN_AMOUNT` | empty (unknown, no hard block) | empty until Stax answers Q25 (`staxMaxTxnAmount`) | the ACH per-transaction limit Stax confirmed; a debit above it cannot be enrolled (PLAN-007c) |
| `STAX_EXPECTED_MERCHANT_ID` | empty, or the sandbox merchant ID | sandbox merchant ID (in `staging.bicepparam`), checked at every start | the merchant ID of the loaded key |
| Config source | `.env`, filled by hand from the vault | Key Vault `finance-sandbox-kv` via managed identity | Key Vault `finance-kv` via managed identity |
| Data | `docker-compose.yml`: PostgreSQL 16 + Redis 7 | Azure resource group `finance-sandbox` | Azure resource group `finance-prod` |
| Banner | always on | always on | on until the live key is wired |
| Hostname | `http://localhost:8000` | `staging.monvera.io` (proposed) | `app.monvera.io` (proposed) |
| Deployment | none | automatic: every push to `main`, after the test job passes | manual only: `workflow_dispatch` job added at Step 16, with a required reviewer |

Deploying anything is a Publish action under [`CLAUDE.md`](CLAUDE.md) §6 and
needs explicit authorisation from the owner in the moment. The step-by-step
runbook — resource groups, deployment commands, vault secret names, the custom
hostname and its DNS records, the GitHub OIDC credential, repository variables
and secrets, and how to give Stax the static egress IP — is in
[`infra/README.md`](infra/README.md).

### Owner checklist for Checkpoint 2

Nothing below can be done by a model: each item is either a cloud action, a
credential, or a decision.

- [ ] **Create the Stax sandbox key** and store it as `stax-api-key-sandbox` in
      both vaults. Until this exists, no code may make a Stax call at all
      (`docs/CLAUDE.md` §4), so staging cannot be exercised end to end.
- [ ] Confirm the sandbox resource group name `finance-sandbox` and the region.
- [ ] **Confirm the hostnames.** `staging.monvera.io` for staging and
      `app.monvera.io` for production are proposals sitting in the parameter
      files; the `monvera.io` zone already exists in `finance-prod`. Binding them
      needs two DNS records and a two-pass deployment, and writing DNS is itself a
      Publish action (`infra/README.md` §4).
- [ ] Create the resource group and the staging vault (`infra/README.md` §1–§2).
- [ ] Set the vault secrets by name: the Stax sandbox key, the webhook URL token,
      the Django secret key, and optionally the Sentry DSN. These must exist
      **before** the environment is deployed (`infra/README.md` §2).
- [ ] Deploy `infra/main.bicep` for staging after reviewing the `what-if` output
      (`infra/README.md` §3).
- [ ] Decide whether Sentry is used now. If yes, create the project and store
      `sentry-dsn`; if no, leave it unset and Sentry stays off.
- [ ] Create the GitHub repository and remote, then the OIDC app registration and
      federated credential, and the `staging` GitHub environment
      (`infra/README.md` §5).
- [ ] Set the GitHub variables `ACR_NAME`, `WEB_APP_NAME`, `WORKER_APP_NAME`
      (default `finance-staging-worker`), `RECONCILE_JOB_NAME` (default
      `finance-staging-reconcile`), `RESOURCE_GROUP`, `APP_BASE_URL` and the
      secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
      `AZURE_SUBSCRIPTION_ID` (`infra/README.md` §6).
- [ ] Decide on IP whitelisting with Stax. If yes, send them the NAT gateway
      address from the deployment output (`infra/README.md` §9); if no, drop the
      NAT gateway and save roughly $35 per month per environment.
- [ ] Verify on staging: `/healthz` reports `"stax_env":"sandbox"`, and
      `/account/login/` shows the undismissable yellow SANDBOX bar with
      `[SANDBOX]` in the page title.

## Start-up and logging

The image applies pending migrations with `deploy_migrate` before it starts
gunicorn, and gunicorn is not started if that fails; on PostgreSQL an advisory lock
makes two replicas that start together migrate one at a time. Then
`provider_environment_check` sends one `GET /self` and refuses to start (exit 3) when
the key's `merchant.is_sandbox` disagrees with `STAX_ENV`, (exit 1) when the merchant is
not `STAX_EXPECTED_MERCHANT_ID`, and (exit 2) when Stax cannot be asked; a new revision
then never becomes ready while the running one keeps serving. Locally without a key it
is skipped. Every WARNING and above,
including each unhandled request error with its traceback, is written once to stderr
(the container log) with the Stax key, the webhook URL token, the Django secret key and
the database and Redis passwords replaced by `[REDACTED]`. A request refused because
its Host is not in `ALLOWED_HOSTS` still gets 400 and is logged as one WARNING line
naming the host (unsafe characters replaced by `?`, at most 100), without a traceback.

## Authentication and roles

Plan: [`docs/qa/PLAN-003-auth-roles.md`](docs/qa/PLAN-003-auth-roles.md). Code:
`backend/accounts/`.

- **Password plus TOTP, always.** Sign-in is `/account/login/`
  (django-two-factor-auth on django-otp). Only authenticator-app (TOTP) devices and
  one-time backup tokens exist; there are no SMS, phone or e-mail methods, and the
  "remember this device" cookie is off (`TWO_FACTOR_REMEMBER_COOKIE_AGE = None`).
  A signed-in user who has not passed the second factor can only enrol an
  authenticator (and only while they have none) or sign out; everything else is 403.
  Django admin requires a verified session (`AdminSiteOTPRequired`).
- **401, not a redirect.** Any request without a session to anything but the login
  flow, `/healthz` or `/static/` gets `401` with a page linking to the login
  (`?next=` is a same-site path only). This includes `/admin/` and `/admin/login/`:
  bookmark `/account/login/`.
- **Roles** are the groups `admin`, `billing` and `viewer` (created by a migration).
  A user's role is their highest group; the superuser flag alone grants nothing.
  Every request other than GET/HEAD/OPTIONS is checked in
  `accounts.middleware.RoleEnforcementMiddleware` against `accounts/registry.py`.
  A route with no rule is refused, and `backend/tests/test_route_registry.py` fails
  when a route is added without being classified. Method-override headers and
  `_method` fields are ignored.
- **Sessions** end after 30 minutes without a request and 12 hours after sign-in
  however active. `POST /account/sign-out-everywhere/` ends every session of the
  current user on every device (CSRF-protected; GET is refused).
- **Local admin.** With `APP_ENV=local` and `LOCAL_ADMIN_PASSWORD` set in your
  shell, `uv run python backend/manage.py seed_local_admin` creates or updates
  `admin@example.com` (staff, `admin` group, not superuser) and prints its
  `otpauth://` URI and five backup tokens. It exits with status 2 in any other
  environment.
- **First admin on staging or production.** The owner (never a model) runs, once,
  in an interactive terminal inside the deployed container:

  ```
  az containerapp exec -g finance-sandbox -n finance-staging-web \
    --command "python backend/manage.py bootstrap_admin --email <owner email>"
  ```

  The password is typed twice at a hidden prompt; there is no password argument or
  environment variable. The account is staff, in the `admin` group, not superuser,
  with no authenticator. Sign in at `/account/login/` and enrol the authenticator
  immediately: until then the account can do nothing else. The command exits with
  status 2 once any admin exists, and in `APP_ENV=local` (use `seed_local_admin`).
  Every later user is created by that admin in the Django admin.

### Route-to-role matrix

"Any" means any signed-in user whose second factor is verified, whatever their
role (enrolment routes also allow a signed-in user who has no device yet).

| Route (name or pattern) | Unsafe methods allowed | Minimum role | Notes |
|---|---|---|---|
| `two_factor:login` | POST | public | the login wizard |
| `healthz` | none | public | GET only; returns `status`, `app_env`, `stax_env` |
| `webhooks:receive` (`/api/webhooks/stax/<token>/`) | POST | public | CSRF-exempt; the URL token is the secret; anything else gets an empty 404 and writes nothing |
| `/static/…` | none | public | static assets |
| `two_factor:setup`, `two_factor:backup_tokens` | POST | any | own authenticator and backup tokens |
| `two_factor:qr`, `two_factor:setup_complete` | none | any | GET only |
| `logout`, `admin:logout` | POST | any | GET refused (405) |
| `accounts:sign_out_everywhere` | POST | any | GET refused (405) |
| `admin:password_change` | POST | viewer | own password |
| `admin:auth_user_*`, `admin:auth_group_*` | POST | admin | user and group management, including `auth_user_password_change` |
| `admin:otp_totp_*`, `admin:otp_static_*` | POST | admin | other users' devices |
| `admin:accounts_usersession_*` | POST | admin | the list itself is read-only |
| `ledger:operators:list`, `ledger:operators:detail` | none | — | GET for any verified user with a role; no role is 403 |
| `ledger:operators:create`, `:edit`, `:retry_sync`, `:link` | POST | billing | GET of the forms for any role |
| `ledger:operators:archive` | POST | admin | soft delete; there is no delete route |
| `admin:ledger_operator_*` | POST | admin | registered read-only: no add, change or delete |
| `ledger:stores:list`, `ledger:stores:detail` | none | — | GET for any verified user with a role |
| `ledger:stores:create`, `:edit` | POST | admin | GET of the forms is admin only too; there is no delete route |
| `admin:ledger_store_*`, `admin:ledger_lease_*` | POST | admin | registered read-only: no add, change or delete |
| `ledger:leases:list`, `ledger:leases:detail` | none | — | GET for any verified user with a role |
| `ledger:leases:create`, `:edit`, `:enrol`, `:pause`, `:resume` | POST | admin | GET of the forms and the enrolment page (which reads Stax) is admin only too |
| `ledger:run:today`, `ledger:charges:detail` | none | — | GET for any verified user with a role; no Stax call |
| `ledger:run:create_charges`, `ledger:run:match` | POST | billing | always for the server's today; matching is refused while the breaker is open or the failure budget is exhausted |
| `ledger:charges:record_payment` | POST | billing | Zelle, wire or other; GET of the form is billing only too; a full payment on a charge with a Stax invoice marks that invoice paid (as cash, with a memo) after re-fetching it; a partial payment makes no Stax call |
| `ledger:webhooks:list`, `ledger:webhooks:detail` | none | — | GET for any verified user with a role; provider IDs masked; the stored body is the redacted copy |
| `ledger:webhooks:replay` | POST | billing | re-runs the handler; handlers are idempotent |
| `ledger:returns:list` | none | — | GET for any verified user with a role: returned ACH payments not yet resolved |
| `ledger:returns:resolve` | POST | billing | a note is required; records who and why; changes no charge; there is no re-debit route |
| `admin:payments_webhookevent_*` | POST | admin | registered read-only: no add, change or delete |
| `ledger:payments:list`, `ledger:payments:export` | none | — | GET for any verified user with a role: the payments ledger with filters and totals, and its CSV export (same filter); provider IDs masked |
| `ledger:payments:detail` | none | — | GET for any verified user with a role: the payment, its parent and refunds, invoice, charge and audit rows; refund and void buttons only for admin |
| `ledger:charges:retry_mark` | POST | billing | retries marking the Stax invoice paid after a failure; refused for 60 seconds after the last attempt |
| `ledger:payments:refund` | POST | admin | GET of the confirmation page is admin only too; settled ACH payments only, up to the remaining refundable amount; exact-value confirmation; calls Stax, re-fetches, audits |
| `ledger:payments:void` | POST | admin | GET of the confirmation page is admin only too; pending or settled ACH payments without refunds, whole amount; the payment becomes `voided` (no new row) |
| `admin:index`, `admin:app_list`, `admin:login`, `admin:jsi18n`, `admin:autocomplete`, `admin:view_on_site`, `admin:password_change_done` | none | — | GET for any verified user; Django admin still checks `is_staff` and model permissions |
| `two_factor:profile`, `two_factor:disable` | none | — | GET only; disabling one's own second factor is refused |
| `ledger:contacts:edit` | POST | billing | the entry's author only, within 15 minutes of recording; `:correct` appends a correction (billing); GET of the forms for billing |
| `ledger:contacts:delete` | POST | admin | soft delete with a mandatory reason; viewers never see the row |
| `ledger:invoices:new_store`, `:new_operator`, `:edit`, `:send`, `:cancel` | POST | billing | the invoice builder (PLAN-012): drafts, one invoice with all lines on Send; GET of the forms is billing only |
| `ledger:invoices:draft`, `:preview`, `:print` | none | — | GET for any verified user with a role |
| `ledger:invoices:charge_now` | POST | admin | the 10d confirmation page; `STAX_PAY_INVOICE_ENABLED` |
| `ledger:one_off_charges:new*` (the old form) | none | — | answers 410 Gone |
| `ledger:disputes:page`, `:export` | none | billing | the dispute list and its CSV; viewer is 403; provider IDs masked |
| `ledger:disputes:refresh` | POST | billing | queues one `disputes_sync` job; BUSY while one waits |
| `ledger:disputes:evidence`, `:evidence_retry` | POST | admin | stores the file as the operator's document (10c checks) and queues the upload; refused after `respond_by` |
| `ledger:stores:end_lease`, `ledger:lease_ends:*` | POST | admin | the end-of-lease wizard (PLAN-012e); GET of every step is admin only too; `:pdf` downloads the stored statement |
| `ledger:leases:stop_autopay` | POST | admin | deletes the lease's provider schedules through the worker; confirmation page on GET (admin) |
| `ledger:stores:transfer`, `ledger:transfers:*` | POST | admin | the operator-transfer wizard (PLAN-012e); `:override_debt` records an admin's reason for a lease-end balance to remain |
| `ledger:charges:split`, `ledger:one_off_charges:split` | POST | billing | Split into installments: preview on GET (billing), confirm on POST (PLAN-012e) |
| `ledger:plans:detail` | GET | viewer | an installment plan's page |
| `ledger:plans:reschedule` | POST | billing | moves the unpaid installments; form on GET (billing) |
| `ledger:plans:cancel` | POST | admin | cancels the plan with a note; form on GET (admin) |
| `ledger:charges:retry_release` | POST | billing | repeats a failed release of a rent charge's Stax invoice or autopay move (PLAN-012b-b D13) |
| `ledger:stores:detail?tab=` | GET | viewer | the store workspace (PLAN-012d): tabs charges, invoices, payments, schedules, documents, contacts, history (admin) |
| `ledger:stores:record_payment` | POST | billing | record a payment for the store and allocate it over its open charges; the remainder as a credit (ADR-0013) |
| `ledger:stores:schedule_payment` | POST | billing | a one-off charge collected by autopay on a chosen date (PLAN-012d) |
| `ledger:invoices:rent_amount` | GET | billing | JSON: the rent amount of a lease and month for the builder's Rent line (PLAN-012d) |
| `ledger:stores:charge_now_pick` | GET | admin | the store's charges eligible for Charge now, each to its confirm page |
| `ledger:invoices:delete_draft` | POST | billing | deletes a never-sent invoice draft (audited) |
| anything else | none | **refused (403)** | an unregistered unsafe route is denied |

The `admin` group also holds Django's model permissions for users, groups, OTP
devices and sessions, and `view` on stores, operators and leases (read-only in the
admin). `billing` and `viewer` hold none yet: they get permissions
with the invoice and customer models (Steps 4+), and settings routes will require
`admin`.

## Status

Prompt 2 (PLAN-002): Django skeleton, environment guard, SANDBOX banner, Sentry
scrubbing, test runners, delivery image, CI and Azure Bicep.

Prompt 3 (PLAN-003): mandatory password + TOTP sign-in, the admin/billing/viewer
roles with a fail-closed route registry, idle and absolute session expiry, sign out
everywhere, and the local-only seeded admin.

Staging is deployed at `https://staging.monvera.io` from `main`; production is not.
PLAN-003a to 003c (migrations on start, error logging, refused-host warnings, first admin)
are deployed. Prompt 4 (PLAN-004): the ledger schema, 23 tables in `backend/ledger/` and
`backend/payments/`, with the card-number guard, exact decimal money and demo data; see
[`docs/DATA_OWNERSHIP.md`](docs/DATA_OWNERSHIP.md) and
[`docs/qa/QA-004-schema.md`](docs/qa/QA-004-schema.md). Prompt 5 (PLAN-005): the `PaymentProvider` interface and the Stax client with its
limiter, breaker, retries, validation and failure budget; see
[`docs/STAX_CLIENT.md`](docs/STAX_CLIENT.md). Prompt 6 (PLAN-006): the operator screens
at `/operators/` (list, search, detail, create, edit, archive, retry, manual link), with
each new or edited operator linked to or created as a Stax customer, the boot-time
environment check, and the reserved `documents` and `outbound_messages` tables; see
[`docs/qa/PLAN-006-operators.md`](docs/qa/PLAN-006-operators.md). Prompt 11b (PLAN-011b): collections ladder by Gmail (delegated service account; catch-all outside
production), NSF re-debit from the returns queue, attention list, contact log, calendar with an
ICS feed, New York time; see
[`docs/qa/PLAN-011b-collections-nsf-attention-calendar.md`](docs/qa/PLAN-011b-collections-nsf-attention-calendar.md)
and ADR-0010.
Prompt 11 (PLAN-011): the dashboard at `/`, the month grid, arrears, the reports menu (aging,
revenue, payments by method, deposits, statement, P&L), CSV on every list, phone layout and
home-screen install (static-only service worker), camera capture; see
[`docs/qa/PLAN-011-dashboard-reports-mobile.md`](docs/qa/PLAN-011-dashboard-reports-mobile.md).
Prompt 10d (PLAN-010d): one-off charges and their Stax invoices from the app (arrears,
adjustments, damage, setup fees; email, manual, autopay or Charge now), cancel, and the
admin audit page at `/audit/` (ADR-0009); see
[`docs/qa/PLAN-010d-one-off-charges-and-audit-page.md`](docs/qa/PLAN-010d-one-off-charges-and-audit-page.md).
Prompt 10c (PLAN-010c): documents on stores and leases in private Blob Storage with the
bank-data check on files (ADR-0008), store-cost corrections, the report timeout; see
[`docs/qa/PLAN-010c-documents-and-cost-corrections.md`](docs/qa/PLAN-010c-documents-and-cost-corrections.md).
Prompt 10b (PLAN-010b): setup fees, store costs, deposits and the P&L report at
`/reports/pnl/`; see [`docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md`](docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md).
Prompt 10 (PLAN-010): the `jobs` table, the `worker` container app as the single Stax
caller, the nightly reconciliation (scheduled job queues, worker runs) and
`/reconciliation/`; see [`docs/qa/PLAN-010-worker-and-reconciliation.md`](docs/qa/PLAN-010-worker-and-reconciliation.md)
and ADR-0006. Current state and open defects: [`docs/STATUS.md`](docs/STATUS.md).

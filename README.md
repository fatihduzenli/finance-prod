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
| `backend/` | Django project: `finance/` (settings, env guard, banner middleware, Sentry, urls), `accounts/` (sign-in, MFA, roles, sessions), `payments/` (reserved for the Stax adapter), `tests/` |
| `infra/` | Azure Bicep templates and the owner's deployment runbook ([`infra/README.md`](infra/README.md)) |
| `scripts/` | Operational scripts, including the `check-no-secrets.sh` build gate |
| `docs/` | Rules, decisions, QA plans and reports (`docs/qa/`) |
| `Dockerfile`, `.dockerignore` | Delivery image: uv build stage, non-root gunicorn runtime |
| `.github/workflows/ci.yml` | Lint, types, unit and e2e tests on every push; staging deploy from `main` |
| `docker-compose.yml` | Local PostgreSQL and Redis |
| `.env.example` | Every environment variable the app reads, with empty values |

## Stack

Python 3.12, Django 5.2 LTS, PostgreSQL 16, Celery + Redis (from Step 10),
`uv` for dependency management, ruff, mypy in strict mode, pytest and
Playwright. Hosted on Azure Container Apps. See ADR-0001 and ADR-0002 in
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
- [ ] Set the GitHub variables `ACR_NAME`, `WEB_APP_NAME`, `RESOURCE_GROUP`,
      `APP_BASE_URL` and the secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
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
makes two replicas that start together migrate one at a time. Every WARNING and above,
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

### Route-to-role matrix

"Any" means any signed-in user whose second factor is verified, whatever their
role (enrolment routes also allow a signed-in user who has no device yet).

| Route (name or pattern) | Unsafe methods allowed | Minimum role | Notes |
|---|---|---|---|
| `two_factor:login` | POST | public | the login wizard |
| `healthz` | none | public | GET only; returns `status`, `app_env`, `stax_env` |
| `/static/…` | none | public | static assets |
| `two_factor:setup`, `two_factor:backup_tokens` | POST | any | own authenticator and backup tokens |
| `two_factor:qr`, `two_factor:setup_complete` | none | any | GET only |
| `logout`, `admin:logout` | POST | any | GET refused (405) |
| `accounts:sign_out_everywhere` | POST | any | GET refused (405) |
| `admin:password_change` | POST | viewer | own password |
| `admin:auth_user_*`, `admin:auth_group_*` | POST | admin | user and group management, including `auth_user_password_change` |
| `admin:otp_totp_*`, `admin:otp_static_*` | POST | admin | other users' devices |
| `admin:accounts_usersession_*` | POST | admin | the list itself is read-only |
| `admin:index`, `admin:app_list`, `admin:login`, `admin:jsi18n`, `admin:autocomplete`, `admin:view_on_site`, `admin:password_change_done` | none | — | GET for any verified user; Django admin still checks `is_staff` and model permissions |
| `two_factor:profile`, `two_factor:disable` | none | — | GET only; disabling one's own second factor is refused |
| anything else | none | **refused (403)** | an unregistered unsafe route is denied |

The `admin` group also holds Django's model permissions for users, groups, OTP
devices and sessions. `billing` and `viewer` hold none yet: they get permissions
with the invoice and customer models (Steps 4+), and refund, void and settings
routes will require `admin`.

## Status

Prompt 2 (PLAN-002): Django skeleton, environment guard, SANDBOX banner, Sentry
scrubbing, test runners, delivery image, CI and Azure Bicep.

Prompt 3 (PLAN-003): mandatory password + TOTP sign-in, the admin/billing/viewer
roles with a fail-closed route registry, idle and absolute session expiry, sign out
everywhere, and the local-only seeded admin.

Staging is deployed at `https://staging.monvera.io` from `main`; production is not.
PLAN-003a (migrations on start, error logging to stderr) passed the QA gate
(`docs/qa/QA-003a-staging-defects.md`); until it is deployed the staging login page
returns 500 because no migration has run there. No Stax API call, no domain model, no Celery yet. Current
state and open defects: [`docs/STATUS.md`](docs/STATUS.md).

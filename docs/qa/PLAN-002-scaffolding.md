# PLAN-002 — Project scaffolding (Prompt 2)

**Author:** Fable (planning, QA). **Implementer:** Opus. **Date:** 2026-09-27.
Governing docs: root `CLAUDE.md`, `docs/CLAUDE.md`, `docs/SCOPE.md`, `docs/DECISIONS.md` (ADR-0001, ADR-0002).

## 1. Objective

Produce a deployable Django skeleton with strict typing, lint, format, unit and end-to-end test
runners; three environments (local, staging with the Stax sandbox key, production with the live
key); a boot-time environment guard that makes sandbox and live impossible to confuse; a permanent
yellow SANDBOX banner on every page when not live; Sentry error monitoring with PII scrubbing;
CI that installs, typechecks, lints and tests on every push and deploys staging from `main` to
Azure Container Apps; a script that fails the build if the Stax API key appears in any
browser-served asset. No Stax API calls, no domain models, no Celery yet.

## 2. Affected areas

In scope: `backend/` (new Django project), root `pyproject.toml`, `uv.lock`, `.env.example`,
`docker-compose.yml`, `Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml`, `infra/`
(Bicep), `scripts/check-no-secrets.sh`, `README.md`.
Out of scope: anything under `backend/payments/` beyond an empty package, Celery, domain
models, migrations of sheet data, `docs/*` (Fable edits docs).

## 3. Interface contract (fixed names; both batches and Fable's tests rely on them)

Toolchain: `uv` (already installed at `~/.local/bin/uv`; prepend to PATH). Python **3.12**.
Django **5.2 LTS**. All commands run from the repo root:

| Purpose | Command |
|---|---|
| Install | `uv sync --all-groups` |
| Lint | `uv run ruff check .` |
| Format check | `uv run ruff format --check .` |
| Types | `uv run mypy` (config in pyproject: `strict = true`, django-stubs plugin, `mypy_path = backend`) |
| Unit tests | `uv run pytest -m "not e2e"` |
| E2E tests | `uv run pytest -m e2e` (pytest-playwright, chromium; `uv run playwright install chromium` once) |
| Dev server | `uv run python backend/manage.py runserver` |
| Secret scan | `scripts/check-no-secrets.sh` (exit 0 clean, exit 1 on a hit) |

Layout:
```
backend/
  manage.py
  finance/                 # Django project package
    __init__.py
    settings.py            # single settings module; reads env; calls env_guard.validate()
    env_guard.py           # pure functions, no Django import: parse + validate env
    middleware.py          # SandboxBannerMiddleware
    sentry.py              # init + before_send scrubber
    urls.py                # admin + /healthz
    asgi.py, wsgi.py
    templates/finance/banner.html
  payments/__init__.py     # empty package, reserved (docs/CLAUDE.md Rule 8)
  tests/
    __init__.py
    conftest.py
    test_env_guard.py
    test_banner.py
    test_sentry_scrub.py
    test_secret_scan.py
    e2e/test_banner_e2e.py
```

Environment variables (server-side only; Django has no browser bundle, and nothing may pass
them to a template context):

| Name | Values | Required |
|---|---|---|
| `APP_ENV` | `local` \| `staging` \| `production` | yes |
| `STAX_ENV` | `sandbox` \| `live` | yes (all APP_ENVs) |
| `STAX_API_BASE_URL` | `https://apiprod.fattlabs.com` | yes |
| `STAX_API_KEY` | secret | yes in staging/production; may be empty in local |
| `STAX_WEBHOOK_URL_TOKEN` | ≥ 64 URL-safe chars | yes in staging/production |
| `DATABASE_URL` | postgres URL; sqlite allowed only when `APP_ENV=local` | yes |
| `REDIS_URL` | reserved for Step 10 | no |
| `APP_BASE_URL` | e.g. `https://staging.example.com` | yes; drives ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS |
| `SENTRY_DSN` | may be empty (Sentry off) | no |
| `DJANGO_SECRET_KEY` | secret | yes |
| `DJANGO_DEBUG` | `0`/`1`; forced `0` unless `APP_ENV=local` | no |

Guard rules (`finance.env_guard.validate(env: Mapping[str,str]) -> Config`, raises
`EnvironmentError` subclass `EnvGuardError` with a value-free message):
1. `APP_ENV` missing/invalid → error.
2. `STAX_ENV` missing/invalid → error, in every `APP_ENV` (docs/CLAUDE.md Rule 6 is stricter than the prompt).
3. `STAX_ENV=live` and `APP_ENV != production` → error ("live key outside production").
4. `STAX_ENV=sandbox` in production → allowed (banner shows). Production's key stays empty until Step 16.
5. `STAX_API_BASE_URL` must equal `https://apiprod.fattlabs.com` exactly unless `APP_ENV=local` (tests point at a fake).
6. `STAX_WEBHOOK_URL_TOKEN` shorter than 64 chars in staging/production → error.
7. `DATABASE_URL` scheme `sqlite` outside local → error.
8. Error messages never include a value. Settings import must fail (process exits non-zero) on any error.

Banner: `SandboxBannerMiddleware` injects, into every `text/html` response whose body contains
`<body`, a `<div>` immediately after the `<body ...>` tag: fixed position top, full width, yellow
`#ffd400` background, black text, `role="status"`, text exactly `SANDBOX — NO REAL MONEY`, no
close control, `id="sandbox-banner"`, and pushes page content down with a body padding rule. It
also prefixes `<title>` with `[SANDBOX] `. Active when `STAX_ENV=sandbox`; absent when `live`.
Applies to admin pages too (admin templates are not overridden; the middleware covers them).

Sentry: `finance.sentry.init(dsn, app_env, stax_env)`; `send_default_pii=False`; `before_send`
removes `request.headers`, `request.cookies`, `request.data`, `request.query_string`,
`user.email`, `user.ip_address`, and any key matching `(?i)(api[_-]?key|authorization|token|secret|password|card|account|routing)`
anywhere in the event, replacing values with `[Filtered]`. Tags: `app_env`, `stax_env`.

Secret scan (`scripts/check-no-secrets.sh`): runs `collectstatic` into a temp dir, then greps
that dir plus `backend/**/templates` and `backend/**/static` for (a) the literal `STAX_API_KEY`
value if the variable is set and non-empty, (b) the regex
`(?i)stax[_-]?api[_-]?key|apiprod\.fattlabs\.com|Bearer [A-Za-z0-9._-]{20,}`. Any hit → prints
file path only (never the match) and exits 1. Runs in CI after tests.

Health: `GET /healthz` returns `{"status":"ok","app_env":...,"stax_env":...}` with no secrets.

## 4. Work breakdown

**Batch A (Opus agent A) — application.** Files: `pyproject.toml`, `uv.lock`, `.env.example`
(rewrite to the table above, empty values, comments), `docker-compose.yml` (postgres 16 +
redis 7 for local), everything under `backend/`, `scripts/check-no-secrets.sh`.
Done when: `uv sync --all-groups`, ruff check, ruff format --check, mypy (strict), and
`pytest -m "not e2e"` all exit 0; `pytest -m e2e` exits 0 with chromium installed; the
secret scan exits 0 on the tree and 1 when a fake key is planted in a static file.

**Batch B (Opus agent B) — delivery.** Files: `Dockerfile`, `.dockerignore`,
`.github/workflows/ci.yml`, `infra/main.bicep`, `infra/modules/*.bicep`,
`infra/parameters/staging.bicepparam`, `infra/parameters/production.bicepparam`,
`infra/README.md`, `README.md` (deployment section). Uses the exact commands in §3 without
running the app. Done when: `ci.yml` is valid YAML with two jobs (`test` on every push and PR;
`deploy-staging` on push to `main`, needs `test`, uses `azure/login@v2` with OIDC, builds with
`az acr build`, updates the `web` container app), and `infra/` describes per environment:
resource group, Log Analytics, Container Apps environment with a NAT gateway for a static egress
IP, ACR, user-assigned identity, Postgres Flexible Server (Burstable B1ms, private), Azure Cache
for Redis Basic C0, Key Vault (staging: new `finance-sandbox-kv`; production: existing
`finance-kv` referenced, not created), and the `web` container app reading secrets as Key Vault
references via the identity. Worker and beat apps are **not** created yet (Step 10) but the
README states that Container Apps supports both always-on replicas and scheduled Jobs.

Batches A and B touch disjoint files and run in parallel.

## 5. Test plan (must pass before acceptance)

Unit (Fable will add adversarial cases in the same files or `tests/test_guard_adversarial.py`):
- guard: each of rules 1–3, 5–7 raises; rule 4 passes; messages contain no env values (assert the
  fake key string is absent from `str(exc)`); `STAX_ENV=" sandbox "` (whitespace) → error;
  `STAX_ENV=SANDBOX` (case) → error; `APP_ENV=prod` → error; webhook token of 63 chars → error,
  64 → ok; sqlite URL in staging → error.
- banner: present on `/admin/login/` and `/healthz`? (healthz is JSON → no banner); present on
  admin login when sandbox; absent when live; not injected into JSON or non-HTML; appears exactly
  once; contains no `button`, `onclick`, or `close`; title prefixed.
- sentry scrubber: an event with headers, cookies, body, an `Authorization` header and a nested
  `stax_api_key` field comes out with all of them `[Filtered]` or removed; a plain message survives.
- secret scan: exit 0 on clean tree; exit 1 when a temp static file contains the configured
  fake key; output does not contain the key.
- no-leak: render admin login page with a distinctive fake `STAX_API_KEY`; assert it is absent.
E2E (Playwright, chromium, `live_server`): open `/admin/login/`, banner visible, fixed to top,
text exact, no element inside it is clickable/removes it after a click anywhere; title prefix.

## 6. Risk register

- Local machine had only Python 3.9; `uv` now provides 3.12. Docker daemon is not running, so
  the image cannot be built locally today; CI builds it.
- No GitHub remote, no Azure resources, no Sentry DSN, no sandbox key exist yet. Deploy and
  "banner visible on staging" cannot be demonstrated in this step; recorded as residual risk.
- A middleware that rewrites HTML could break streaming responses: skip `StreamingHttpResponse`.
- `APP_BASE_URL` drives ALLOWED_HOSTS; a wrong value locks staff out. Local defaults to
  `http://localhost:8000`.
- Personal-data files under `docs/inputs/private/` are git-ignored; nothing here reads them.

## 7. Acceptance criteria

1. Commands in §3 all exit 0 on a clean checkout with `APP_ENV=local STAX_ENV=sandbox` and a
   sqlite `DATABASE_URL`.
2. `APP_ENV=production` with `STAX_ENV` unset: `python backend/manage.py check` exits non-zero
   and prints a message without any env value.
3. `APP_ENV=staging STAX_ENV=live`: same non-zero exit.
4. Sandbox: admin login HTML contains exactly one `id="sandbox-banner"` with the exact text and
   no interactive element; live: none.
5. `scripts/check-no-secrets.sh` exits 0; planting the key in a static file makes it exit 1.
6. `.env` and `.env.*` (except `.env.example`) are git-ignored; `.env.example` has empty values.
7. No template or static file references `STAX_API_KEY`, `STAX_WEBHOOK_URL_TOKEN`, or the
   base URL.
8. `ci.yml` parses; `test` job runs the §3 commands; `deploy-staging` is gated on `main` and on
   `test`; no secret literal in the workflow.
9. Bicep compiles is **not** verifiable locally (no `az`/`bicep`); reviewed by reading.

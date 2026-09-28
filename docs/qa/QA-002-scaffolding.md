## QA Report — PLAN-002 Project scaffolding (Prompt 2)

**Date:** 2026-09-27. **Gate run by:** Fable, on this machine, after Opus reported completion.
**Plan:** `docs/qa/PLAN-002-scaffolding.md`.

Scope:            Root: `pyproject.toml`, `uv.lock`, `.env.example`, `docker-compose.yml`, `Dockerfile`,
                  `.dockerignore`, `.github/workflows/ci.yml`. `backend/` (Django project `finance`,
                  `payments` placeholder package, 9 test modules incl. 2 by Fable and 1 e2e).
                  `infra/` (main.bicep, 13 modules, 2 param files, README). `scripts/check-no-secrets.sh`.
                  `README.md`. Docs by Fable: `docs/CLAUDE.md` §4 env-var table, DNS note; this report.

Commands run:     (env: APP_ENV=local STAX_ENV=sandbox STAX_API_BASE_URL=https://apiprod.fattlabs.com
                  DATABASE_URL=sqlite:///:memory: APP_BASE_URL=http://localhost:8000
                  DJANGO_SECRET_KEY=<placeholder> STAX_API_KEY=<fake>)
                  uv sync --all-groups ........................ exit 0
                  uv run ruff check . ......................... exit 0
                  uv run ruff format --check . ................ exit 0
                  uv run mypy (strict, django-stubs) .......... exit 0, 22 files
                  uv run pytest -m "not e2e" -q ............... exit 0, 111 passed
                  uv run pytest -m e2e -q ..................... exit 0, 1 passed (chromium)
                  bash scripts/check-no-secrets.sh ............ exit 0
                  same, fake key planted in backend/finance/static exit 1, key value absent from output
                  APP_ENV=production, STAX_ENV unset, manage.py check ... exit 1, value-free message
                  APP_ENV=staging, STAX_ENV=live, manage.py check ........ exit 1, value-free message
                  docker compose config -q .................... exit 0
                  ci.yml YAML parse ........................... ok; deploy-staging needs test, gated on main push

Tests:            112 total (111 unit + 1 e2e), 112 pass, 0 skipped, 0 flaky (two runs identical).
                  Coverage: not measured (no coverage tool in the approved dependency list; add in Step 5).
                  Fable-authored adversarial files: test_guard_adversarial.py (37 cases),
                  test_banner_adversarial.py (8 cases). One of them found a real defect (F1).

Findings:         2 MAJOR, both resolved before sign-off. 0 BLOCKER. 4 MINOR recorded, deferred.
  F1 [MAJOR, resolved] env_guard.Config repr printed the Stax key, webhook token, DB URL, DSN and
     Django secret. Fixed with field(repr=False); covered by test_config_repr_does_not_leak_secrets.
  F2 [MAJOR, resolved] /healthz was subject to SECURE_SSL_REDIRECT outside local, so in-replica
     probes over plain HTTP got a 301. Fixed with SECURE_REDIRECT_EXEMPT; covered by
     test_settings_deployed.py (subprocess import under staging env + middleware-level 200/301 tests).
  F3 [MINOR, deferred] GitHub Actions (checkout@v4, setup-uv@v5, azure/login@v2) are tag-pinned,
     not SHA-pinned. Decision: accept for now; pin by SHA before production deploys (Step 16).
  F4 [MINOR, deferred] `on: push` on all branches plus `pull_request` runs CI twice for PR branches.
     Accept; cost only.
  F5 [MINOR, deferred] Bicep is unverified by a compiler (no az/bicep locally). First `what-if` is
     the owner's. Highest-risk properties are listed in infra/README.md and the Batch B report:
     managedEnvironment.customDomainConfiguration.customDomainVerificationId, managedCertificates
     property names, NAT gateway on a workload-profile environment.
  F6 [MINOR, deferred] Local checkout path ends in a space; gunicorn strips it and refuses to start
     from this directory. Container WORKDIR is unaffected. Owner may rename the folder.

Gate 3 notes:     Readability and pattern adherence good. No dead code. Deviations 1–9 in Opus's report
                  accepted (notably: conftest-driven Django setup, DJANGO_ALLOW_ASYNC_UNSAFE in e2e only,
                  compressed non-manifest static storage, deploy hardening block, extra guard rules).
                  Scope discipline: no Stax call, no Celery, no domain model (as planned).

Gate 4 / §5.4:    Secret-pattern scan over all 66 files to be committed: clean. No .env present;
                  .env is ignored, .env.example tracked with empty values only. `git log --all -- .env`
                  empty. No secret-store read by either model. New credential-capable file type:
                  `.bicepparam` — committed by design, contains secret NAMES only (verified by grep);
                  policy: values never go in param files, vault references only. Every new secret name is
                  documented in .env.example or infra/README.md with no value. Webhook path token is not
                  logged (gunicorn access log off; documented in Dockerfile).
                  Browser exposure: Django has no client bundle; no context processor exposes settings;
                  test_no_template_or_static_references_secret_settings and the scan script enforce it.

Residual risk:    NOT demonstrated, because the accounts/resources do not exist yet (owner actions):
                  - Staging auto-deploy from main (no GitHub remote, no Azure resource group, no OIDC).
                  - SANDBOX banner visible on staging (same).
                  - Docker image build (daemon not running); base image tags verified to exist in the
                    registries but the build itself is untested. First build happens in CI.
                  - Bicep compile and deployment (F5).
                  - Postgres path: tests ran on sqlite; CI uses a postgres service container.
                  - Sentry: exercised only in the DSN-empty path; scrubber unit-tested with synthetic events.

Verdict:          PASS for local acceptance criteria 1–7 of PLAN-002 §7. Criteria "staging deploys
                  automatically from main" and "banner visible on staging" are UNVERIFIED and remain
                  open until the owner completes Checkpoint 2. No Publish Request is submitted: nothing
                  here pushes, deploys or creates resources.

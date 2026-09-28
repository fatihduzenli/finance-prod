## QA Report — PLAN-003 Authentication, mandatory MFA, roles (Prompt 3)

**Date:** 2026-09-28. **Gate run by:** Fable on this machine after Opus reported completion.
**Plan:** `docs/qa/PLAN-003-auth-roles.md`.

Scope:            New app `backend/accounts/` (roles, registry, four middleware classes, UserSession
                  model + 2 migrations, 401/403/MFA templates, sign-out-everywhere view, seed command,
                  two_factor base-template override without CDN assets). `backend/finance/settings.py`
                  (django-otp, two_factor, session policy, LOGIN_URL), `backend/finance/urls.py`.
                  `pyproject.toml`/`uv.lock` (django-otp 1.7.3, django-two-factor-auth 1.18.1, qrcode 8.2;
                  transitive django-formtools, django-phonenumber-field). `.env.example` (+LOCAL_ADMIN_PASSWORD).
                  README (Authentication and roles section with the matrix). Tests: 9 Opus modules + 1 e2e,
                  and Fable's `backend/tests/test_auth_adversarial.py` (12 cases). Two PLAN-002 banner tests
                  re-pointed at `/account/login/` because `/admin/login/` now answers 401 (accepted).

Commands run:     (placeholder env as in QA-002)
                  uv sync --all-groups ........................ exit 0 (42 packages)
                  uv run ruff check . ......................... exit 0
                  uv run ruff format --check . ................ exit 0 (65 files)
                  uv run mypy ................................. exit 0 (48 files, strict)
                  uv run pytest -q ............................ exit 0, 208 passed (two full runs, identical)
                  bash scripts/check-no-secrets.sh ............ exit 0
                  manage.py makemigrations --check --dry-run .. exit 0
                  seed_local_admin under APP_ENV=production ... exit 2, nothing created
                  enumeration negative proof (Opus, dummy unregistered POST route) ... test fails as required

Tests:            208 total (196 Opus + 12 Fable), 208 pass, 0 skipped, 0 flaky. Coverage not measured
                  (tool not in the approved list; add at Step 5).
                  Fable adversarial cases, all refused server-side: password-only user cannot mint backup
                  tokens (0 tokens created); password-only user with a device cannot enrol another; verified
                  user cannot disable own MFA; superuser without group cannot add users; billing with all
                  model permissions still 403 on user add; group added/removed mid-session honoured at once;
                  401 page reflects no markup and no secrets; login `next=` cannot leave the site; registry
                  refuses unknown/unnamed routes and non-registered methods for every role; no rule grants
                  mutation to anonymous; deleting the verifying device revokes access on the next request;
                  sign-out-everywhere refuses GET (405) and a POST without CSRF (403).

Findings:         0 BLOCKER, 0 MAJOR. 3 MINOR recorded.
  F1 [MINOR, deferred] MFA_SETUP_ROUTE_NAMES lists two_factor:backup_tokens as reachable by an
     unverified user with no device. The library's own otp_required decorator refuses it (verified by
     test), so there is no exposure; tighten the allow-list at Step 5 for clarity.
  F2 [MINOR, deferred] sign-out-everywhere decodes every live session (O(n)). Fine at staff scale.
  F3 [MINOR, deferred] Public route `/healthz` is an addition to the prompt's "login and webhook only"
     list, chosen for platform probes and CI smoke. Owner confirmed 2026-09-28; recorded as ADR-0003. Closed.

Gate 3 notes:     Deviations 1–12 in Opus's report accepted, including the admin group's model
                  permissions (needed for a non-superuser admin to manage users) and `min_role=None` for
                  routes any signed-in user must reach. Server-side check for a specific route, read by
                  Fable: `accounts/middleware.py` RoleEnforcementMiddleware.process_view →
                  `registry.rule_for("admin:auth_user_add")` → ADMIN_MODEL_PREFIX_RULES → Role.ADMIN.

Gate 4 / §5.4:    Secret-pattern scan over all files: clean. No credential literal; test password and
                  seed password are placeholders/env-driven. No secret-store read. New file types: none.
                  No new secret name beyond LOCAL_ADMIN_PASSWORD (documented, empty). Remember-device
                  cookie is None (asserted by test). No phone/SMS method installed (asserted by test).

Residual risk:    Postgres not exercised locally (CI does). Cookie Secure flag path outside local not
                  exercised end-to-end. Staging MFA enrolment end-to-end (Checkpoint 3) waits on the
                  staging deployment. Container image still unbuilt locally (Docker daemon off).

Verdict:          PASS for PLAN-003 acceptance criteria 1–6. Owner read the admin:auth_user_add check on 2026-09-28 (Checkpoint 3
                  item confirmed). Staging enrolment, the viewer destructive attempt on staging and the
                  admin list remain open until staging exists.

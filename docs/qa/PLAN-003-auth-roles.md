# PLAN-003 — Authentication, mandatory MFA, roles (Prompt 3)

**Author:** Fable. **Implementer:** Opus. **Date:** 2026-09-28.
Governing docs: root `CLAUDE.md`, `docs/CLAUDE.md`, `docs/SCOPE.md` §9 (roles decision: admin / billing / viewer), PLAN-002.

## 1. Objective

Every staff user signs in with a password **and** a TOTP second factor; no path lets a user
reach any non-public route without both. Three roles (admin, billing, viewer) are enforced
server-side in middleware for every mutating request, with a registry that fails closed: a
mutating route that is not registered is refused, and a test fails if a new route appears
without a registry entry. Sessions expire on inactivity and on an absolute ceiling, and a user
can end every session they hold. A local-only seeded admin exists and cannot be created outside
`APP_ENV=local`. Unauthenticated requests to non-public routes get 401, not a redirect.

## 2. Provider decision (Prompt 3 "stop and ask" clause)

Django has no built-in second factor. The standard, free, self-hosted stack is **django-otp**
(TOTP devices, static backup tokens) plus **django-two-factor-auth** (login flow, enrolment,
QR setup, backup tokens). Both are open source, no paid tier, no external service. Constraint
to neutralise: two-factor-auth's optional "remember this device" cookie must be **off**
(`TWO_FACTOR_REMEMBER_COOKIE_AGE = None`), because it would let a device skip the second factor.
Phone/SMS methods are not installed. No stop needed on this clause.

## 3. Public routes (Prompt 3 "stop and ask" clause)

Public = reachable without a session: the login flow (`/account/login/` and its two-factor
steps), static assets, and **`/healthz`**. The prompt names only login and the future webhook
receiver. `/healthz` is added because Container Apps probes and the CI smoke test call it
unauthenticated; it returns only `status`, `app_env`, `stax_env`. **Flagged to the owner in the
Prompt 3 report; one line to remove if refused.** Everything else, including `/admin/` and
`/account/two_factor/setup/`, requires a session (setup additionally requires an
authenticated-but-not-yet-verified user, see §4).

## 4. Interface contract

Dependencies (add, pinned `==`): `django-otp`, `django-two-factor-auth` (with `qrcode`), and
`django-otp`'s static plugin. No `phonenumbers`, no SMS/voice gateway.

Package layout: new app `backend/accounts/`:
```
accounts/
  __init__.py, apps.py
  models.py         # UserSession (user FK, session_key unique, created_at, last_seen_at, user_agent[:200], ip?) — no IP (PII), keep user_agent only
  roles.py          # Role enum {ADMIN="admin", BILLING="billing", VIEWER="viewer"}; group names; role_of(user) -> Role | None; RANK admin > billing > viewer
  registry.py       # ROUTE_ROLES: dict[str, RouteRule]; RouteRule(min_role: Role, methods: frozenset[str]); helpers is_public(path), rule_for(request)
  middleware.py     # AuthRequiredMiddleware (401 on non-public without user), MfaRequiredMiddleware (authenticated but not otp-verified -> only setup/logout allowed, else 403 + redirect hint), RoleEnforcementMiddleware (process_view: unsafe method -> registry lookup -> 403 when below min_role or unregistered), SessionPolicyMiddleware (idle + absolute timeout)
  signals.py        # on user_logged_in: create UserSession; on user_logged_out: delete
  views.py          # sign_out_everywhere (POST only, any authenticated role), 401 page view
  admin.py          # register UserSession read-only; AdminSite uses two_factor's AdminSiteOTPRequired
  management/commands/seed_local_admin.py
  migrations/0001_initial.py
  templates/accounts/{401.html, signed_out_everywhere.html}
  templates/two_factor/...  # only if a template override is needed for the banner/title
```

Settings additions:
- `INSTALLED_APPS` += `django_otp`, `django_otp.plugins.otp_totp`, `django_otp.plugins.otp_static`, `two_factor`, `accounts`.
- `MIDDLEWARE` order (after Session/Auth): `django_otp.middleware.OTPMiddleware`, `accounts.middleware.SessionPolicyMiddleware`, `accounts.middleware.AuthRequiredMiddleware`, `accounts.middleware.MfaRequiredMiddleware`, `accounts.middleware.RoleEnforcementMiddleware`.
- `LOGIN_URL = "two_factor:login"`, `LOGIN_REDIRECT_URL = "/admin/"`, `TWO_FACTOR_REMEMBER_COOKIE_AGE = None`, `OTP_TOTP_ISSUER = "Finance (SANDBOX)"` when sandbox else `"Finance"`.
- Sessions: DB backend; `SESSION_COOKIE_AGE = 30*60` idle with `SESSION_SAVE_EVERY_REQUEST = True`; absolute ceiling `SESSION_ABSOLUTE_MAX_AGE = 12*60*60` enforced by `SessionPolicyMiddleware` via a `login_at` key set at login; both values as settings constants; `SESSION_COOKIE_SAMESITE = "Strict"`.
- Admin site: replace default `admin.site.__class__` with `AdminSiteOTPRequired` so `/admin/login/` redirects to the two-factor login and admin views require a verified user.

Roles: Django `Group`s named `admin`, `billing`, `viewer`, created by a data migration. A user
belongs to exactly one; `role_of()` returns the highest if several. Superuser flag alone does
**not** grant role (registry looks at groups only); the seed command adds the seeded user to
`admin`. `is_staff=True` is required for admin access and is set by the seed command; for other
users the owner sets it in admin.

Registry (fail closed): keyed by resolved URL name with namespace (`admin:auth_user_add`) or a
prefix rule for admin model routes (`admin:<app>_<model>_<action>`). Initial matrix:

| Route (name or pattern) | Methods | Min role | Notes |
|---|---|---|---|
| `two_factor:login`, `two_factor:setup`, `two_factor:setup_complete`, `two_factor:qr`, `two_factor:backup_tokens` | GET, POST | any authenticated (setup: unverified allowed) | login is public |
| `logout` | POST | any | GET refused |
| `accounts:sign_out_everywhere` | POST | any | deletes all UserSession rows and sessions for the user |
| `admin:index`, `admin:app_list`, `admin:*_changelist`, `admin:*_change` (GET), `admin:*_history`, `admin:jsi18n` | GET | viewer | read-only |
| `admin:auth_user_*` (add, change POST, delete, password_change), `admin:auth_group_*`, `admin:accounts_usersession_*` | POST | admin | user management |
| `admin:otp_totp_*`, `admin:otp_static_*` | POST | admin | device management for others |
| `admin:password_change` (own) | POST | viewer | own password |
| `healthz` | GET | public | see §3 |
| any other unsafe request | * | **refused 403** | unregistered = denied, and the enumeration test fails |

Refunds, voids, invoices, customers, settings do not exist yet; when they do (Steps 4+), their
routes are added to the registry with `admin` for refund/void/settings and `billing` for
customers/invoices/send/record-payment. The prompt's "billing gets 403 from refund and
settings" test is therefore written against the registry helper with synthetic rules today and
against real routes later; document that in the test.

Seed command `seed_local_admin`: refuses with exit 2 unless `settings.APP_ENV == "local"`;
requires `LOCAL_ADMIN_PASSWORD` env (no default); creates or updates `admin@example.com`
(`is_staff`, `is_superuser=False`, group `admin`), a confirmed TOTP device, prints the
`otpauth://` URI and 5 static backup tokens to stdout with a loud "LOCAL ONLY" banner. The
command is also blocked by a test that imports it under `APP_ENV=staging` (subprocess) and
expects exit 2 and no user created.

401 handling: `AuthRequiredMiddleware` returns `HttpResponse(status=401)` rendering
`accounts/401.html` with a link to the login page carrying `next`. Header
`WWW-Authenticate: Session` is not set (no basic auth).

## 5. Work breakdown (one Opus agent; files are interdependent)

1. Dependencies + settings + accounts app skeleton + migrations (groups data migration).
2. Middleware quartet + registry + roles.
3. Views (401 page, sign-out-everywhere), signals, UserSession model, admin wiring
   (`AdminSiteOTPRequired`).
4. Seed command.
5. Tests (Opus): login without OTP cannot reach `/admin/` (401/403); OTP-verified user can; viewer
   GET admin index 200; viewer POST to every registered mutating route 403; billing 403 on
   synthetic refund/settings rules; unauthenticated 401 on every non-public route (enumerated by
   walking `urlpatterns`); public routes exactly {login flow, healthz, static}; session idle
   expiry; absolute expiry; sign-out-everywhere kills a second session; seed command refused
   outside local; remember-cookie setting is None; `two_factor` has no phone methods installed.
6. Enumeration test: walk `get_resolver().url_patterns` recursively, build the set of
   `(namespace:name)`; every name must be either public, GET-only-safe (viewer), or present in
   `ROUTE_ROLES`; the test asserts the registry has no stale names and every unsafe route is
   registered. Adding an unregistered route makes it fail.
7. README: status update + "Authentication and roles" section with the matrix; `.env.example`
   gains `LOCAL_ADMIN_PASSWORD` (empty).

## 6. Test plan (Fable adds adversarial cases after Opus's run)

Adversarial: role escalation via multiple groups; superuser without group gets 403 on mutation;
verified-then-device-deleted user loses access on next request; `next=` open redirect on login
(must be relative); 401 body contains no secrets; POST with method override headers
(`X-HTTP-Method-Override`) is not honoured; HEAD/OPTIONS treated as safe; a route registered
with role but reached via a different namespace; session cookie flags; logout by GET refused;
sign-out-everywhere by GET refused and requires CSRF; MFA setup page itself is not reachable
without a session; seed command with `APP_ENV=production` in a subprocess.

## 7. Risks

- django-two-factor-auth's admin integration and template names change between versions; pin
  and read the installed package before writing templates.
- `SESSION_SAVE_EVERY_REQUEST` with DB sessions adds a write per request; acceptable at this
  scale.
- The 401 policy changes the admin UX (no auto-redirect). Documented in README.
- Playwright e2e for the full TOTP login is worth one test (`pyotp` is not in the list; use
  `django_otp.oath.totp` from the installed package to compute the code).

## 8. Acceptance criteria

1. All PLAN-002 commands still exit 0; new tests pass.
2. Registry enumeration test passes now and fails when a dummy unsafe route is added without a
   rule (demonstrated by Opus and by Fable).
3. `TWO_FACTOR_REMEMBER_COOKIE_AGE is None`; no template or setting exposes a skip path.
4. `seed_local_admin` exits 2 under staging/production in a subprocess and creates nothing.
5. Unauthenticated: every non-public route 401; public set is exactly login flow, static,
   healthz.
6. Viewer: 403 on every registered mutating route, 200 on admin index.

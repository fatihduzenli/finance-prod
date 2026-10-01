"""Django settings. One module for every environment; behaviour comes from APP_ENV.

The environment is validated at import time (``finance.env_guard``). An invalid
environment exits the process instead of starting a web server, a worker or a
management command with a configuration that could send real money (docs/CLAUDE.md
Rule 6).

Secrets live in settings only. Nothing here may be passed to a template context, and
no context processor may expose it: the Stax key must never reach a browser.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url

from finance import log_redaction, sentry
from finance.env_guard import EnvGuardError, validate

# backend/
BASE_DIR = Path(__file__).resolve().parent.parent

try:
    _config = validate(os.environ)
except EnvGuardError as error:
    # The message names variables only, never values.
    print(f"Environment guard failed: {error}", file=sys.stderr)
    raise SystemExit(1) from error

APP_ENV = _config.app_env
STAX_ENV = _config.stax_env
STAX_API_BASE_URL = _config.stax_api_base_url
#: Server-side only. Never rendered, logged or exported.
STAX_API_KEY = _config.stax_api_key
#: Server-side only. Becomes the secret path segment for inbound webhooks (Rule 4).
STAX_WEBHOOK_URL_TOKEN = _config.stax_webhook_url_token
#: "worker" only in the one process that owns the Stax rate-limit bucket (PLAN-005 D7).
STAX_CALLER = _config.stax_caller
#: Gate for the pay-an-invoice call (PLAN-005 D3). Off unless explicitly "true".
STAX_PAY_INVOICE_ENABLED = _config.stax_pay_invoice_enabled
#: Merchant the key must belong to; empty means the check is not configured (Rule 6).
STAX_EXPECTED_MERCHANT_ID = _config.stax_expected_merchant_id
#: "stax" (default) or "fake", the in-memory provider, local only (PLAN-006 §4.2).
STAX_PROVIDER = _config.stax_provider
#: A Decimal; debits above it are flagged on the enrolment screen (PLAN-007 §4.3).
#: A placeholder until Stax states the per-transaction limit (SCOPE Q25).
LARGE_DEBIT_WARNING_AMOUNT = _config.large_debit_warning_amount
APP_BASE_URL = _config.app_base_url
REDIS_URL = _config.redis_url

SECRET_KEY = _config.django_secret_key
DEBUG = _config.debug

_base_url = urlsplit(APP_BASE_URL)
ALLOWED_HOSTS = [_base_url.hostname] if _base_url.hostname else []
CSRF_TRUSTED_ORIGINS = [f"{_base_url.scheme}://{_base_url.netloc}"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Mandatory TOTP second factor (docs/qa/PLAN-003-auth-roles.md §2). Only the TOTP and
    # static (backup token) plugins are installed: no phone, SMS or e-mail methods.
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    # accounts precedes two_factor so its template overrides (two_factor/_base.html)
    # win over the package defaults under APP_DIRS.
    "accounts",
    "two_factor",
    # Business ledger and the provider-side tables (docs/qa/PLAN-004-schema.md).
    "ledger",
    "payments",
    # Project package as an app so its management commands (deploy_migrate) are found.
    # It has no models. Listed last so it changes no template or static resolution.
    "finance",
]

# SandboxBannerMiddleware sits high in the list so that, on the way out, it runs after
# every other middleware and sees the finished HTML.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "finance.middleware.SandboxBannerMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Order matters: OTPMiddleware adds request.user.is_verified(); the session policy
    # expires stale sessions before the authentication check sees them; then 401 for
    # anonymous, 403 for unverified, and the per-route role check (PLAN-003 §4).
    "django_otp.middleware.OTPMiddleware",
    "accounts.middleware.SessionPolicyMiddleware",
    "accounts.middleware.AuthRequiredMiddleware",
    "accounts.middleware.MfaRequiredMiddleware",
    "accounts.middleware.RoleEnforcementMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "finance.urls"
WSGI_APPLICATION = "finance.wsgi.application"
ASGI_APPLICATION = "finance.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "finance" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            # Deliberately the Django defaults only. No project context processor
            # exists, so no setting can leak into a rendered page.
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": dj_database_url.parse(_config.database_url, conn_max_age=600)}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "finance" / "static"]
# STATIC_ROOT is an output directory, not behaviour: scripts/check-no-secrets.sh
# overrides it with a temporary directory so it can scan a fresh collectstatic.
STATIC_ROOT = Path(os.environ.get("STATIC_ROOT") or BASE_DIR / "staticfiles")
# Local development and the test suite serve files from disk as they are; deployed
# images run collectstatic at build time, so scanning STATIC_ROOT at startup is right
# there and only there.
WHITENOISE_AUTOREFRESH = APP_ENV == "local"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Compressed but not hashed: the app is served without a build step, and a
    # manifest would make every page depend on collectstatic having run.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_HTTPONLY = True

# --- Authentication (docs/qa/PLAN-003-auth-roles.md) ---------------------------------
LOGIN_URL = "two_factor:login"
LOGIN_REDIRECT_URL = "/admin/"
LOGOUT_REDIRECT_URL = "two_factor:login"
# "Remember this device" would let a browser skip the second factor. Off, always.
TWO_FACTOR_REMEMBER_COOKIE_AGE = None
OTP_TOTP_ISSUER = "Finance (SANDBOX)" if STAX_ENV == "sandbox" else "Finance"

# Sessions live in the database. Idle timeout: the cookie and the stored session expire
# 30 minutes after the last request, because every request re-saves the session.
# Absolute ceiling: accounts.middleware.SessionPolicyMiddleware ends any session 12 hours
# after sign-in, however active.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_ABSOLUTE_MAX_AGE = 12 * 60 * 60
SESSION_COOKIE_SAMESITE = "Strict"

if APP_ENV != "local":
    # Container Apps terminates TLS in front of the app.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    # Platform probes and the image HEALTHCHECK reach /healthz over plain HTTP inside
    # the replica, with no X-Forwarded-Proto, so it must not be redirected.
    SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31_536_000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# --- Logging (docs/qa/PLAN-003a-staging-defects.md §3) ---------------------------------
# One handler, stderr, the same in every APP_ENV. Every WARNING and above, including
# django.request's unhandled exceptions with their traceback, is written once. The
# formatter removes the secret values below from the finished line and traceback.
# "django" and "django.server" lose Django's default handlers (console, mail_admins,
# the runserver handler) and propagate to the root handler instead, so nothing is
# written twice and nothing bypasses redaction.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "redacting": {
            "()": "finance.log_redaction.RedactingFormatter",
            "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
            # A frozenset, not a list or string: logging.config would otherwise
            # inspect the values for ext:// and cfg:// references.
            "secrets": log_redaction.secret_values(
                stax_api_key=STAX_API_KEY,
                stax_webhook_url_token=STAX_WEBHOOK_URL_TOKEN,
                django_secret_key=SECRET_KEY,
                database_url=_config.database_url,
                redis_url=REDIS_URL,
            ),
        },
    },
    "filters": {
        "refused_host": {"()": "finance.log_redaction.RefusedHostFilter"},
    },
    "handlers": {
        "stderr": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "redacting",
        },
    },
    "root": {"handlers": ["stderr"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": [], "level": "WARNING", "propagate": True},
        # runserver only (local): its request lines, redacted, instead of its own handler.
        "django.server": {"handlers": [], "level": "INFO", "propagate": True},
        # A request for a host not in ALLOWED_HOSTS: one WARNING line, no traceback
        # (docs/qa/PLAN-003b). Only this logger; other django.security.* are unchanged.
        "django.security.DisallowedHost": {
            "handlers": [],
            "filters": ["refused_host"],
            "propagate": True,
        },
        # Payments at INFO: one redacted line per Stax request reaches stderr (PLAN-005).
        "payments": {"handlers": [], "level": "INFO", "propagate": True},
    },
}

sentry.init(dsn=_config.sentry_dsn, app_env=APP_ENV, stax_env=STAX_ENV)

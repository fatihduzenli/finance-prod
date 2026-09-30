"""Boot-time environment validation.

Pure module by design: it imports no Django and reads no process state itself, so
``settings`` can call it before Django is configured and tests can call it with a
plain dict. Rules are fixed by ``docs/qa/PLAN-002-scaffolding.md`` §3 and
``docs/CLAUDE.md`` Rule 6 (sandbox and live must be impossible to confuse).

Messages name variables only. A bad value may itself be a live API key, so no value
read from the environment is ever echoed (root ``CLAUDE.md`` §5.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

APP_ENVS: tuple[str, ...] = ("local", "staging", "production")
DEPLOYED_APP_ENVS: tuple[str, ...] = ("staging", "production")
STAX_ENVS: tuple[str, ...] = ("sandbox", "live")
#: Which process may call Stax (docs/qa/PLAN-005-stax-client.md D7). Unset means "none".
STAX_CALLERS: tuple[str, ...] = ("none", "worker")
#: Which PaymentProvider the app uses (docs/qa/PLAN-006-operators.md §4.2). Unset means
#: "stax"; "fake" is the in-memory provider and is accepted only when APP_ENV is local.
STAX_PROVIDERS: tuple[str, ...] = ("stax", "fake")
#: STAX_PAY_INVOICE_ENABLED (PLAN-005 D3). Unset means "false".
BOOLEAN_FLAGS: tuple[str, ...] = ("true", "false")

#: Sandbox and live share this host; only the key differs (docs/CLAUDE.md §1).
REQUIRED_STAX_API_BASE_URL = "https://apiprod.fattlabs.com"

WEBHOOK_URL_TOKEN_MIN_LENGTH = 64

_SQLITE_SCHEME_PREFIX = "sqlite"
_WEB_URL_SCHEMES: tuple[str, ...] = ("http", "https")


class EnvGuardError(EnvironmentError):
    """The environment is not a configuration this app is allowed to start with."""


@dataclass(frozen=True, slots=True)
class Config:
    """Validated environment. Values are verbatim; nothing is normalised.

    Every field that carries a credential is excluded from ``repr`` so that a
    traceback, an f-string or a Sentry breadcrumb holding a ``Config`` cannot print
    it (root ``CLAUDE.md`` §5.2). Attribute access is unaffected. ``database_url``
    and ``sentry_dsn`` count: both embed a key or a password.
    """

    app_env: str
    stax_env: str
    stax_api_base_url: str
    stax_api_key: str = field(repr=False)
    stax_webhook_url_token: str = field(repr=False)
    database_url: str = field(repr=False)
    redis_url: str
    app_base_url: str
    sentry_dsn: str = field(repr=False)
    django_secret_key: str = field(repr=False)
    debug: bool
    stax_caller: str
    stax_pay_invoice_enabled: bool
    #: Not a credential, but identifying: shown masked only, so kept out of ``repr``.
    stax_expected_merchant_id: str = field(repr=False)
    stax_provider: str


def validate(env: Mapping[str, str]) -> Config:
    """Return the validated configuration, or raise :class:`EnvGuardError`.

    All problems are collected so one boot attempt reports the whole list.
    """
    app_env = env.get("APP_ENV", "")
    stax_env = env.get("STAX_ENV", "")
    stax_api_base_url = env.get("STAX_API_BASE_URL", "")
    stax_api_key = env.get("STAX_API_KEY", "")
    stax_webhook_url_token = env.get("STAX_WEBHOOK_URL_TOKEN", "")
    database_url = env.get("DATABASE_URL", "")
    app_base_url = env.get("APP_BASE_URL", "")
    django_secret_key = env.get("DJANGO_SECRET_KEY", "")
    stax_caller = env.get("STAX_CALLER", "") or "none"
    stax_pay_invoice_enabled = env.get("STAX_PAY_INVOICE_ENABLED", "") or "false"
    stax_provider = env.get("STAX_PROVIDER", "") or "stax"

    is_local = app_env == "local"
    is_deployed = app_env in DEPLOYED_APP_ENVS

    problems: list[str] = []

    # Rule 1: APP_ENV.
    if app_env not in APP_ENVS:
        problems.append(f"APP_ENV is missing or not one of: {', '.join(APP_ENVS)}")

    # Rule 2: STAX_ENV is required in every APP_ENV (docs/CLAUDE.md Rule 6).
    if stax_env not in STAX_ENVS:
        problems.append(f"STAX_ENV is missing or not one of: {', '.join(STAX_ENVS)}")

    # Rule 3: a live key may only be loaded by production. Rule 4: sandbox is
    # allowed everywhere, including production, where the banner then shows.
    if stax_env == "live" and app_env != "production":
        problems.append("STAX_ENV may only be 'live' when APP_ENV is 'production'")

    # Rule 5: the base URL is pinned outside local; local may point at a fake.
    if not stax_api_base_url:
        problems.append("STAX_API_BASE_URL is missing")
    elif not is_local and stax_api_base_url != REQUIRED_STAX_API_BASE_URL:
        problems.append(
            "STAX_API_BASE_URL must be exactly the documented Stax base URL "
            "unless APP_ENV is 'local'"
        )

    # Rule 6: webhook URL token length, in deployed environments only.
    if is_deployed and len(stax_webhook_url_token) < WEBHOOK_URL_TOKEN_MIN_LENGTH:
        problems.append(
            f"STAX_WEBHOOK_URL_TOKEN is missing or shorter than "
            f"{WEBHOOK_URL_TOKEN_MIN_LENGTH} characters"
        )

    # The key is required where real calls are possible; local may run without one.
    if is_deployed and not stax_api_key:
        problems.append("STAX_API_KEY is missing")

    # Rule 7: sqlite is a local-only convenience.
    if not database_url:
        problems.append("DATABASE_URL is missing")
    else:
        database_scheme = urlsplit(database_url).scheme.lower()
        if not database_scheme:
            problems.append("DATABASE_URL must be a URL with a scheme")
        elif database_scheme.startswith(_SQLITE_SCHEME_PREFIX) and not is_local:
            problems.append("DATABASE_URL may only use a sqlite scheme when APP_ENV is 'local'")

    # Drives ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS; a broken value locks staff out.
    if not app_base_url:
        problems.append("APP_BASE_URL is missing")
    else:
        parsed_base = urlsplit(app_base_url)
        if parsed_base.scheme.lower() not in _WEB_URL_SCHEMES or not parsed_base.hostname:
            problems.append("APP_BASE_URL must be an absolute http(s) URL including a host")

    if not django_secret_key:
        problems.append("DJANGO_SECRET_KEY is missing")

    # PLAN-005 D7: only the process that owns the Stax rate-limit bucket calls Stax.
    if stax_caller not in STAX_CALLERS:
        problems.append(f"STAX_CALLER is not one of: {', '.join(STAX_CALLERS)}")

    # PLAN-005 D3: the pay-an-invoice call is off unless explicitly enabled.
    if stax_pay_invoice_enabled not in BOOLEAN_FLAGS:
        problems.append(f"STAX_PAY_INVOICE_ENABLED is not one of: {', '.join(BOOLEAN_FLAGS)}")

    # PLAN-006 §4.2: the in-memory provider is for local development only.
    if stax_provider not in STAX_PROVIDERS:
        problems.append(f"STAX_PROVIDER is not one of: {', '.join(STAX_PROVIDERS)}")
    elif stax_provider == "fake" and not is_local:
        problems.append("STAX_PROVIDER may only be 'fake' when APP_ENV is 'local'")

    if problems:
        raise EnvGuardError("; ".join(problems))

    return Config(
        app_env=app_env,
        stax_env=stax_env,
        stax_api_base_url=stax_api_base_url,
        stax_api_key=stax_api_key,
        stax_webhook_url_token=stax_webhook_url_token,
        database_url=database_url,
        redis_url=env.get("REDIS_URL", ""),
        app_base_url=app_base_url,
        sentry_dsn=env.get("SENTRY_DSN", ""),
        django_secret_key=django_secret_key,
        # Never on outside local, whatever DJANGO_DEBUG says.
        debug=is_local and env.get("DJANGO_DEBUG", "") == "1",
        stax_caller=stax_caller,
        stax_pay_invoice_enabled=stax_pay_invoice_enabled == "true",
        stax_expected_merchant_id=env.get("STAX_EXPECTED_MERCHANT_ID", ""),
        stax_provider=stax_provider,
    )

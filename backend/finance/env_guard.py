"""Boot-time environment validation.

Pure module by design: it imports no Django and reads no process state itself, so
``settings`` can call it before Django is configured and tests can call it with a
plain dict. Rules are fixed by ``docs/qa/PLAN-002-scaffolding.md`` §3 and
``docs/CLAUDE.md`` Rule 6 (sandbox and live must be impossible to confuse).

Messages name variables only. A bad value may itself be a live API key, so no value
read from the environment is ever echoed (root ``CLAUDE.md`` §5.2).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

#: PLAN-007 §4.3: a debit above this amount shows a warning on the enrolment screen.
#: A placeholder until Stax states the per-transaction limit (SCOPE Q25).
DEFAULT_LARGE_DEBIT_WARNING_AMOUNT = "2000.00"
#: PLAN-007c §1: STAX_MAX_TXN_AMOUNT is the ACH per-transaction limit confirmed by Stax
#: (SCOPE Q25). Empty means unknown: no hard block.
_CENT = Decimal("0.01")

#: PLAN-010 D12: the reconciliation's thresholds. Name -> (default, smallest, largest).
#: Each is a whole number; anything else aborts the boot. The day windows stay under a
#: year because the provider's date-range queries span at most one.
RECONCILE_INTEGERS: dict[str, tuple[int, int, int]] = {
    "PENDING_PAYMENT_MAX_BUSINESS_DAYS": (5, 1, 60),
    "RECONCILE_LOOKBACK_DAYS": (35, 1, 364),
    "RECONCILE_OVERLAP_HOURS": (48, 0, 720),
    "RECONCILE_DEPOSIT_DAYS": (14, 1, 364),
}
#: PLAN-010c D11: the PostgreSQL statement timeout of the P&L page and export, in
#: milliseconds (default, smallest, largest).
REPORT_STATEMENT_TIMEOUT_MS = (15_000, 1_000, 120_000)
DEFAULT_RECONCILE_LOCAL_TIME = "02:00"
#: PLAN-011b D1: the project's time zone (``settings.TIME_ZONE``). Storage stays UTC
#: (``USE_TZ``); every "today", charge period, debit day and date taken from a provider
#: timestamp is a New York date.
PROJECT_TIME_ZONE = "America/New_York"
#: The reconciliation's zone defaults to the project's (PLAN-011b D1).
DEFAULT_RECONCILE_TIMEZONE = PROJECT_TIME_ZONE
_WHOLE_NUMBER = re.compile(r"[0-9]{1,6}")
_LOCAL_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
#: An IANA zone name such as America/New_York: no path, no dots at the start.
_ZONE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*")

#: PLAN-010c D1, D2: the document storage account's blob endpoint, the container and the
#: managed identity's client ID. Empty URL: the documents feature is off outside local.
#: The endpoint is exactly ``https://<account>.blob.core.windows.net`` (a trailing slash,
#: as Azure reports the endpoint, is accepted); an account name is 3 to 24 lower-case
#: letters and digits.
_BLOB_ACCOUNT_URL = re.compile(r"https://[a-z0-9]{3,24}\.blob\.core\.windows\.net/?")
DEFAULT_DOCUMENTS_CONTAINER = "documents"
#: An Azure container name: 3 to 63 lower-case letters, digits and single hyphens,
#: starting and ending with a letter or digit.
_CONTAINER_NAME = re.compile(r"(?!.*--)[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_CLIENT_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: PLAN-011b D3, PLAN-014 D1: outbound mail. ``MAIL_ENABLED`` is off unless "true"; when it
#: is on, the service-account key is required (the mailbox it sends as is the company
#: settings' sender address, and on the sandbox only the allowlist of the company settings is
#: reached: neither is an environment variable). The key is checked to be a JSON object
#: holding these keys; only missing key names are ever reported, never a value.
SERVICE_ACCOUNT_KEYS: tuple[str, ...] = ("client_email", "private_key", "token_uri")
#: PLAN-011b D4: the ``{company}`` merge field. Plain text, one line.
DEFAULT_COMPANY_NAME = "Global Retail Groups"
COMPANY_NAME_MAX_LENGTH = 200
_ONE_LINE = re.compile(r"[^\x00-\x1f\x7f]+")

#: PLAN-012f-b D13: the USPS APIs' production host. ``USPS_API_BASE_URL`` may differ
#: only when APP_ENV is local, so tests and a local run can point at a fake.
DEFAULT_USPS_API_BASE_URL = "https://apis.usps.com"

#: PLAN-014 C7: the processor's web payments token for its hosted bank-account fields.
#: Optional (empty: bank entry is not set up). It is written into the bank-entry page, so
#: only these characters are accepted; it is a secret everywhere else.
_WEB_PAYMENTS_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,255}")

#: PLAN-014 I1: the bank-deposits connector's environment. ``PLAID_ENV`` names Plaid's host
#: (empty: sandbox); production is refused when APP_ENV is local. The client ID, the secret
#: and the access tokens' encryption key are optional as a set of three: all set or none.
PLAID_ENVS: tuple[str, ...] = ("sandbox", "development", "production")
DEFAULT_PLAID_ENV = "sandbox"
PLAID_SECRET_NAMES: tuple[str, ...] = ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_TOKEN_KEY")
#: A Fernet key is 32 bytes in URL-safe base64 (44 characters with its padding).
_FERNET_KEY_BYTES = 32

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
    large_debit_warning_amount: Decimal
    #: ``None`` when the confirmed provider limit is unknown (PLAN-007c §1).
    stax_max_txn_amount: Decimal | None
    #: PLAN-010 D12.
    pending_payment_max_business_days: int
    reconcile_lookback_days: int
    reconcile_overlap_hours: int
    reconcile_deposit_days: int
    reconcile_local_time: time
    reconcile_timezone: str
    #: PLAN-010c D2. Identifiers, not credentials: no key or token is ever configured.
    documents_storage_account_url: str = ""
    documents_container: str = DEFAULT_DOCUMENTS_CONTAINER
    azure_client_id: str = ""
    #: PLAN-010c D11.
    report_statement_timeout_ms: int = REPORT_STATEMENT_TIMEOUT_MS[0]
    #: PLAN-011b D3. The key is a credential: kept out of ``repr``.
    mail_enabled: bool = False
    gmail_service_account_json: str = field(default="", repr=False)
    #: PLAN-011b D4.
    company_name: str = DEFAULT_COMPANY_NAME
    #: PLAN-012f-b D13: the USPS Addresses API's OAuth client. Both empty: the address
    #: check is off. Both are credentials (the ID too, as USPS issues them as a pair):
    #: kept out of ``repr``.
    usps_client_id: str = field(default="", repr=False)
    usps_client_secret: str = field(default="", repr=False)
    usps_api_base_url: str = DEFAULT_USPS_API_BASE_URL
    #: PLAN-014 C7: empty means the hosted bank-account fields are not set up.
    stax_web_payments_token: str = field(default="", repr=False)
    #: PLAN-014 I1: the bank-deposits connector. The three below are credentials (the client
    #: ID too, as Plaid issues it with the secret): kept out of ``repr``. All empty: no keys.
    plaid_env: str = DEFAULT_PLAID_ENV
    plaid_client_id: str = field(default="", repr=False)
    plaid_secret: str = field(default="", repr=False)
    plaid_token_key: str = field(default="", repr=False)


def _positive_amount(value: str) -> Decimal | None:
    """A positive, finite amount with at most two decimal places, or ``None``."""
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(_CENT):
        return None
    return amount


def _whole_number(value: str, smallest: int, largest: int) -> int | None:
    if not _WHOLE_NUMBER.fullmatch(value):
        return None
    number = int(value)
    return number if smallest <= number <= largest else None


def _local_time(value: str) -> time | None:
    match = _LOCAL_TIME.fullmatch(value)
    return time(int(match.group(1)), int(match.group(2))) if match else None


def _zone(value: str) -> str | None:
    if not _ZONE_NAME.fullmatch(value):
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return value


def _service_account_problem(raw: str) -> str | None:
    """Why the service-account key is unusable, naming keys only; ``None`` when usable."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return "GMAIL_SERVICE_ACCOUNT_JSON is not valid JSON"
    if not isinstance(parsed, dict):
        return "GMAIL_SERVICE_ACCOUNT_JSON must be a JSON object"
    missing = [
        name
        for name in SERVICE_ACCOUNT_KEYS
        if not isinstance(parsed.get(name), str) or not parsed.get(name)
    ]
    if missing:
        return f"GMAIL_SERVICE_ACCOUNT_JSON lacks the keys: {', '.join(missing)}"
    return None


def _mail_problems(env: Mapping[str, str]) -> tuple[list[str], bool, str, str]:
    """PLAN-011b D3, D4, PLAN-014 D1: the mail settings' problems, then the values."""
    problems: list[str] = []
    mail_enabled = env.get("MAIL_ENABLED", "") or "false"
    if mail_enabled not in BOOLEAN_FLAGS:
        problems.append(f"MAIL_ENABLED is not one of: {', '.join(BOOLEAN_FLAGS)}")
    enabled = mail_enabled == "true"
    key = env.get("GMAIL_SERVICE_ACCOUNT_JSON", "")
    if enabled and not key:
        problems.append("GMAIL_SERVICE_ACCOUNT_JSON is required when MAIL_ENABLED is true")
    if key:
        problem = _service_account_problem(key)
        if problem is not None:
            problems.append(problem)
    company_name = env.get("COMPANY_NAME", "") or DEFAULT_COMPANY_NAME
    if len(company_name) > COMPANY_NAME_MAX_LENGTH or not _ONE_LINE.fullmatch(company_name):
        problems.append(
            f"COMPANY_NAME must be one line of at most {COMPANY_NAME_MAX_LENGTH} characters"
        )
    return problems, enabled, key, company_name


def _usps_problems(env: Mapping[str, str], is_local: bool) -> tuple[list[str], str, str, str]:
    """PLAN-012f-b D13: the USPS client ID and secret are optional as a pair; the base URL
    is the USPS host unless APP_ENV is local. Only variable names are reported, never a
    value."""
    problems: list[str] = []
    client_id = env.get("USPS_CLIENT_ID", "")
    client_secret = env.get("USPS_CLIENT_SECRET", "")
    if bool(client_id) != bool(client_secret):
        problems.append(
            "USPS_CLIENT_ID and USPS_CLIENT_SECRET must be set together or both left empty"
        )
        client_id = client_secret = ""
    base_url = env.get("USPS_API_BASE_URL", "") or DEFAULT_USPS_API_BASE_URL
    if base_url != DEFAULT_USPS_API_BASE_URL:
        parsed = urlsplit(base_url)
        if not is_local:
            problems.append(
                "USPS_API_BASE_URL must be exactly the USPS host unless APP_ENV is 'local'"
            )
        elif parsed.scheme.lower() not in _WEB_URL_SCHEMES or not parsed.hostname:
            problems.append("USPS_API_BASE_URL must be an absolute http(s) URL including a host")
    return problems, client_id, client_secret, base_url


def _is_fernet_key(value: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False
    return len(decoded) == _FERNET_KEY_BYTES


def _plaid_problems(env: Mapping[str, str], is_local: bool) -> tuple[list[str], str, str, str, str]:
    """PLAN-014 I1: the Plaid environment and its three secrets, all set or none; the key a
    Fernet key. Only variable names are reported, never a value."""
    problems: list[str] = []
    plaid_env = env.get("PLAID_ENV", "") or DEFAULT_PLAID_ENV
    if plaid_env not in PLAID_ENVS:
        problems.append(f"PLAID_ENV is not one of: {', '.join(PLAID_ENVS)}")
    elif plaid_env == "production" and is_local:
        problems.append("PLAID_ENV may not be 'production' when APP_ENV is 'local'")
    client_id, secret, token_key = (env.get(name, "") for name in PLAID_SECRET_NAMES)
    present = [bool(client_id), bool(secret), bool(token_key)]
    if any(present) and not all(present):
        problems.append(
            "PLAID_CLIENT_ID, PLAID_SECRET and PLAID_TOKEN_KEY must be set together or all "
            "left empty"
        )
        client_id = secret = token_key = ""
    if token_key and not _is_fernet_key(token_key):
        problems.append("PLAID_TOKEN_KEY must be a Fernet key (32 bytes in URL-safe base64)")
    return problems, plaid_env, client_id, secret, token_key


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
    large_debit_warning_amount = _positive_amount(
        env.get("LARGE_DEBIT_WARNING_AMOUNT", "") or DEFAULT_LARGE_DEBIT_WARNING_AMOUNT
    )
    raw_max_txn_amount = env.get("STAX_MAX_TXN_AMOUNT", "")
    stax_max_txn_amount = _positive_amount(raw_max_txn_amount) if raw_max_txn_amount else None

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

    # PLAN-007 §4.3: the large-debit warning threshold.
    if large_debit_warning_amount is None:
        problems.append(
            "LARGE_DEBIT_WARNING_AMOUNT must be a positive amount with at most two decimals"
        )

    # PLAN-007c §1: the confirmed provider limit, optional.
    if raw_max_txn_amount and stax_max_txn_amount is None:
        problems.append("STAX_MAX_TXN_AMOUNT must be a positive amount with at most two decimals")

    # PLAN-010 D12: the reconciliation's thresholds, schedule and time zone.
    reconcile: dict[str, int] = {}
    for name, (default, smallest, largest) in RECONCILE_INTEGERS.items():
        number = _whole_number(env.get(name, "") or str(default), smallest, largest)
        if number is None:
            problems.append(f"{name} must be a whole number from {smallest} to {largest}")
        else:
            reconcile[name] = number
    reconcile_local_time = _local_time(
        env.get("RECONCILE_LOCAL_TIME", "") or DEFAULT_RECONCILE_LOCAL_TIME
    )
    if reconcile_local_time is None:
        problems.append("RECONCILE_LOCAL_TIME must be a time of day as HH:MM")
    reconcile_timezone = _zone(env.get("RECONCILE_TIMEZONE", "") or DEFAULT_RECONCILE_TIMEZONE)
    if reconcile_timezone is None:
        problems.append("RECONCILE_TIMEZONE must be a known IANA time zone name")

    # PLAN-010c D11: the report's statement timeout.
    timeout_default, timeout_smallest, timeout_largest = REPORT_STATEMENT_TIMEOUT_MS
    report_statement_timeout_ms = _whole_number(
        env.get("REPORT_STATEMENT_TIMEOUT_MS", "") or str(timeout_default),
        timeout_smallest,
        timeout_largest,
    )
    if report_statement_timeout_ms is None:
        problems.append(
            f"REPORT_STATEMENT_TIMEOUT_MS must be a whole number from {timeout_smallest} "
            f"to {timeout_largest}"
        )

    # PLAN-010c D2: document storage. Empty URL means the feature is off (outside local).
    documents_storage_account_url = env.get("DOCUMENTS_STORAGE_ACCOUNT_URL", "")
    documents_container = env.get("DOCUMENTS_CONTAINER", "") or DEFAULT_DOCUMENTS_CONTAINER
    azure_client_id = env.get("AZURE_CLIENT_ID", "")
    if documents_storage_account_url and not _BLOB_ACCOUNT_URL.fullmatch(
        documents_storage_account_url
    ):
        problems.append(
            "DOCUMENTS_STORAGE_ACCOUNT_URL must be https://<account>.blob.core.windows.net "
            "when it is set"
        )
    if not _CONTAINER_NAME.fullmatch(documents_container):
        problems.append(
            "DOCUMENTS_CONTAINER must be 3 to 63 lower-case letters, digits and single hyphens"
        )
    if azure_client_id and not _CLIENT_ID.fullmatch(azure_client_id):
        problems.append("AZURE_CLIENT_ID must be a client ID in UUID form")
    elif documents_storage_account_url and not azure_client_id:
        problems.append("AZURE_CLIENT_ID is required when DOCUMENTS_STORAGE_ACCOUNT_URL is set")

    mail_problems, mail_enabled, gmail_key, company_name = _mail_problems(env)
    problems.extend(mail_problems)

    usps_problems, usps_client_id, usps_client_secret, usps_api_base_url = _usps_problems(
        env, is_local
    )
    problems.extend(usps_problems)

    # PLAN-014 C7: the hosted fields' token, optional; never with the in-memory provider
    # (the browser would create methods at the processor the fake knows nothing of).
    stax_web_payments_token = env.get("STAX_WEB_PAYMENTS_TOKEN", "")
    if stax_web_payments_token:
        if not _WEB_PAYMENTS_TOKEN.fullmatch(stax_web_payments_token):
            problems.append(
                "STAX_WEB_PAYMENTS_TOKEN must be letters, digits, '.', '_' or '-' "
                "(at most 255) when it is set"
            )
        if stax_provider == "fake":
            problems.append("STAX_WEB_PAYMENTS_TOKEN must be empty when STAX_PROVIDER is 'fake'")

    plaid_problems, plaid_env, plaid_client_id, plaid_secret, plaid_token_key = _plaid_problems(
        env, is_local
    )
    problems.extend(plaid_problems)

    if (
        problems
        or large_debit_warning_amount is None
        or reconcile_local_time is None
        or reconcile_timezone is None
        or report_statement_timeout_ms is None
    ):
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
        large_debit_warning_amount=large_debit_warning_amount,
        stax_max_txn_amount=stax_max_txn_amount,
        pending_payment_max_business_days=reconcile["PENDING_PAYMENT_MAX_BUSINESS_DAYS"],
        reconcile_lookback_days=reconcile["RECONCILE_LOOKBACK_DAYS"],
        reconcile_overlap_hours=reconcile["RECONCILE_OVERLAP_HOURS"],
        reconcile_deposit_days=reconcile["RECONCILE_DEPOSIT_DAYS"],
        reconcile_local_time=reconcile_local_time,
        reconcile_timezone=reconcile_timezone,
        documents_storage_account_url=documents_storage_account_url,
        documents_container=documents_container,
        azure_client_id=azure_client_id,
        report_statement_timeout_ms=report_statement_timeout_ms,
        mail_enabled=mail_enabled,
        gmail_service_account_json=gmail_key,
        company_name=company_name,
        usps_client_id=usps_client_id,
        usps_client_secret=usps_client_secret,
        usps_api_base_url=usps_api_base_url,
        stax_web_payments_token=stax_web_payments_token,
        plaid_env=plaid_env,
        plaid_client_id=plaid_client_id,
        plaid_secret=plaid_secret,
        plaid_token_key=plaid_token_key,
    )

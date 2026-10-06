"""The single HTTP choke point for Stax (docs/CLAUDE.md Rule 3, PLAN-005 §4.2, D5 to D8).

:meth:`StaxTransport.send` runs, in this order, for every attempt: the caller check
(D7), the circuit breaker, the rate limiter, the request, classification of the
result, the breaker and budget update, one log line, and the retry decision (D6).

The ``httpx`` client has its own retries set to 0 and redirects off, so no retry and no
extra request can hide beneath this one (D5). Timeouts are 15 s on connect, read,
write and pool.

Log lines are built from the method, the path template, the status, the duration, the
object ID and, on failure, the class name of the failure. Nothing else: never a body,
a header, the token, an e-mail address or an amount (Rule 1).

Two things are read from a body, and only key names or one identifier (PLAN-006a):

- on a 4xx, the key names of the error body (``error_fields=``), reduced to
  ``[A-Za-z0-9_.-]`` and cut, from at most the first 64 KB; never a value;
- on a 2xx to one of the create paths, the top-level ``id`` (``object_id=``) when it is
  a plain identifier of at most 64 characters.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import quote

import httpx
from django.conf import settings

from payments.errors import (
    ProviderNotPermittedHere,
    ProviderRejected,
    ProviderUnavailable,
    TransportError,
)
from payments.stax import mapping
from payments.stax.breaker import CircuitBreaker, Outcome, get_breaker
from payments.stax.budget import FailureBudget, get_budget
from payments.stax.clock import SYSTEM_CLOCK, Clock
from payments.stax.limiter import DEFAULT_MAX_WAIT_MS, RateLimiter, get_limiter

logger = logging.getLogger(__name__)

WORKER = "worker"
TIMEOUT_SECONDS = 15
#: D6: one attempt, then retries after 1 s, 4 s and 15 s.
BACKOFF_MS: tuple[int, ...] = (1_000, 4_000, 15_000)
MAX_ATTEMPTS = len(BACKOFF_MS) + 1
TOO_MANY_REQUESTS = 429
ID_PLACEHOLDER = "{id}"

#: PLAN-006a §3.1: what is read of a 4xx body, and how its key names are reduced.
ERROR_BODY_MAX_BYTES = 64 * 1024
ERROR_FIELD_MAX_LENGTH = 40
ERROR_FIELDS_MAX = 20
#: Keys whose value is an object whose own keys are reported too.
ERROR_CONTAINERS: tuple[str, ...] = ("errors", "error")
#: A reduced key with this many digits could be a card or account number.
ERROR_FIELD_MAX_DIGITS = 12
#: Minimum length of a reduced key that is checked against the API key as a fragment.
SECRET_FRAGMENT_MIN_LENGTH = 8
NON_JSON = "non-json"
LIST_BODY = "list"
SCALAR_BODY = "scalar"
REDACTED_FIELD = "redacted"
_UNSAFE_KEY_CHARACTERS = re.compile(r"[^A-Za-z0-9_.-]")

#: PLAN-006a §3.2: POST paths whose 2xx response ``id`` is logged as ``object_id``.
CREATE_PATHS = frozenset(
    {
        "/customer",
        "/invoice",
        "/invoice/schedule/",
        "/invoice/{id}/pay",
        "/transaction/{id}/refund",
        "/transaction/{id}/void",
    }
)
_CREATED_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"


class RetryPolicy(StrEnum):
    """Which failed attempts :meth:`StaxTransport.send` repeats."""

    #: D6: a 429 always; a 5xx or a network error only for an idempotent call.
    STANDARD = "standard"
    #: PLAN-010d D4: exactly one HTTP request, whatever the outcome (pay-an-invoice).
    NONE = "none"


@dataclass(frozen=True, slots=True)
class _Attempt:
    outcome: Outcome
    status: int | None
    response: httpx.Response | None
    failure: str
    #: Key names of a 4xx body (PLAN-006a §3.1); ``None`` when not a 4xx.
    error_fields: tuple[str, ...] | None = None
    #: The decoded body of a 2xx.
    data: object = None
    #: A 2xx whose body is not valid JSON.
    unreadable: bool = False


def build_client(
    base_url: str, api_key: str, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """The one ``httpx`` client. ``transport`` is injected by tests only."""
    return httpx.Client(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(TIMEOUT_SECONDS),
        follow_redirects=False,
        transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
    )


class StaxTransport:
    def __init__(
        self,
        *,
        http_transport: httpx.BaseTransport | None = None,
        limiter: RateLimiter | None = None,
        breaker: CircuitBreaker | None = None,
        budget: FailureBudget | None = None,
        clock: Clock = SYSTEM_CLOCK,
        max_wait_ms: int = DEFAULT_MAX_WAIT_MS,
        api_key: str | None = None,
    ) -> None:
        # ``api_key`` overrides settings.STAX_API_KEY for the sandbox test suites only.
        self._api_key = api_key
        self._http_transport = http_transport
        self._client: httpx.Client | None = None
        self._limiter = limiter
        self._breaker = breaker
        self._budget = budget
        self.clock = clock
        self.max_wait_ms = max_wait_ms

    # Collaborators are resolved lazily, so building an adapter in a process that may
    # not call Stax touches neither the network nor the database.

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter or get_limiter()

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker or get_breaker()

    @property
    def budget(self) -> FailureBudget:
        return self._budget or get_budget()

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            api_key = self._configured_key()
            if not api_key:
                raise ProviderNotPermittedHere("No provider API key is configured")
            self._client = build_client(settings.STAX_API_BASE_URL, api_key, self._http_transport)
        return self._client

    def _configured_key(self) -> str:
        return str(self._api_key if self._api_key is not None else settings.STAX_API_KEY or "")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # --- The choke point -------------------------------------------------------------

    def send(
        self,
        method: Method,
        path: str,
        *,
        object_id: str = "",
        params: Mapping[str, str | int | bool] | None = None,
        json: Mapping[str, object] | None = None,
        idempotent: bool,
        boot_check: bool = False,
        retry: RetryPolicy = RetryPolicy.STANDARD,
    ) -> object:
        """Send one logical request; return the decoded JSON body (``None`` if empty).

        ``path`` is a template; ``{id}`` is replaced by the percent-encoded
        ``object_id``. ``idempotent=False`` marks a call that may have taken effect even
        when no response arrived: it is retried after a 429 only (D6).

        ``boot_check=True`` is passed by ``provider_environment_check`` only, for its one
        ``GET /self`` per process start (PLAN-006 D2). It skips the ``STAX_CALLER`` gate
        and nothing else: the breaker, limiter, budget, retry policy and logging apply.

        ``retry=RetryPolicy.NONE`` sends exactly one request: no 429, 5xx or network
        error is repeated (the caller check, breaker, limiter, budget and logging still
        apply).
        """
        if not boot_check and settings.STAX_CALLER != WORKER:
            raise ProviderNotPermittedHere("This process is not permitted to call the provider")
        client = self.client
        url = path.replace(ID_PLACEHOLDER, quote(object_id, safe="")) if object_id else path
        content = mapping.encode_json(json) if json is not None else None

        attempts = 1 if retry is RetryPolicy.NONE else MAX_ATTEMPTS
        for attempt in range(1, attempts + 1):
            result = self._attempt(client, method, url, path, object_id, params, content)
            if result.outcome is Outcome.SUCCESS:
                if result.unreadable:
                    raise ProviderUnavailable(
                        result.status, "The provider response was not valid JSON"
                    )
                return result.data
            if result.outcome is Outcome.CLIENT_ERROR and result.status is not None:
                raise ProviderRejected(result.status, error_fields=result.error_fields or ())
            retryable = result.outcome is Outcome.THROTTLED or (
                idempotent and result.outcome in (Outcome.SERVER_ERROR, Outcome.NETWORK_ERROR)
            )
            if not retryable or attempt == attempts:
                if result.outcome is Outcome.NETWORK_ERROR:
                    raise TransportError()
                raise ProviderUnavailable(result.status)
            self.clock.sleep_ms(BACKOFF_MS[attempt - 1])
        raise AssertionError("unreachable")  # pragma: no cover

    def _attempt(
        self,
        client: httpx.Client,
        method: Method,
        url: str,
        path: str,
        object_id: str,
        params: Mapping[str, str | int | bool] | None,
        content: bytes | None,
    ) -> _Attempt:
        breaker = self.breaker
        breaker.before_call()
        try:
            self.limiter.acquire(self.max_wait_ms)
            started = self.clock.monotonic_ms()
            try:
                response = client.request(
                    method.value, url, params=dict(params) if params else None, content=content
                )
            except httpx.TransportError as error:
                result = _Attempt(Outcome.NETWORK_ERROR, None, None, type(error).__name__)
            except httpx.HTTPError as error:
                result = _Attempt(Outcome.SERVER_ERROR, None, None, type(error).__name__)
            else:
                result = _classify(response, self._configured_key())
        except BaseException:
            breaker.abandon()
            raise
        elapsed_ms = self.clock.monotonic_ms() - started
        breaker.record(result.outcome)
        if result.outcome is not Outcome.SUCCESS:
            self.budget.record_http_failure()
        elif result.response is not None:
            result = _decoded(result, result.response)
        logged_id = object_id
        if result.outcome is Outcome.SUCCESS and method is Method.POST and path in CREATE_PATHS:
            logged_id = created_id(result.data) or object_id
        _log(method, path, result, elapsed_ms, logged_id)
        return result


def _classify(response: httpx.Response, api_key: str) -> _Attempt:
    status = response.status_code
    if 200 <= status < 300:
        return _Attempt(Outcome.SUCCESS, status, response, "")
    if status == TOO_MANY_REQUESTS:
        return _Attempt(
            Outcome.THROTTLED,
            status,
            response,
            "ProviderUnavailable",
            error_fields=error_fields(response.content, api_key),
        )
    if 400 <= status < 500:
        return _Attempt(
            Outcome.CLIENT_ERROR,
            status,
            response,
            "ProviderRejected",
            error_fields=error_fields(response.content, api_key),
        )
    # 5xx, and anything else unexpected (an unfollowed redirect, a 1xx).
    return _Attempt(Outcome.SERVER_ERROR, status, response, "ProviderUnavailable")


def _decoded(result: _Attempt, response: httpx.Response) -> _Attempt:
    """A 2xx with its body decoded once, or marked unreadable."""
    try:
        return replace(result, data=mapping.decode_json(response.content))
    except ValueError:
        return replace(result, unreadable=True)


def error_fields(body: bytes, api_key: str = "") -> tuple[str, ...]:
    """Key names of a 4xx body, never a value (PLAN-006a §3.1).

    Top-level keys of a JSON object, plus the keys of an object under ``errors`` or
    ``error``; ``list`` or ``scalar`` for any other JSON; ``non-json`` otherwise (also
    for a body cut at :data:`ERROR_BODY_MAX_BYTES`, or nested too deeply to parse).
    Each key is reduced by :func:`safe_field_name`; at most :data:`ERROR_FIELDS_MAX`,
    sorted.
    """
    try:
        parsed = json.loads(body[:ERROR_BODY_MAX_BYTES])
    except (ValueError, RecursionError):
        return (NON_JSON,)
    if isinstance(parsed, list):
        return (LIST_BODY,)
    if not isinstance(parsed, dict):
        return (SCALAR_BODY,)
    keys: list[str] = list(parsed)
    for container in ERROR_CONTAINERS:
        nested = parsed.get(container)
        if isinstance(nested, dict):
            keys.extend(nested)
    names = {name for name in (safe_field_name(key, api_key) for key in keys) if name}
    return tuple(sorted(names)[:ERROR_FIELDS_MAX])


def safe_field_name(key: str, api_key: str = "") -> str:
    """``key`` reduced to ``[A-Za-z0-9_.-]`` and cut; empty if nothing is left.

    A key that holds the API key (or a long fragment of it), or that holds more than
    :data:`ERROR_FIELD_MAX_DIGITS` digits, becomes ``redacted``.
    """
    reduced = _UNSAFE_KEY_CHARACTERS.sub("", key)[:ERROR_FIELD_MAX_LENGTH]
    if not reduced:
        return ""
    if api_key and (
        api_key in key
        or api_key in reduced
        or (len(reduced) >= SECRET_FRAGMENT_MIN_LENGTH and reduced in api_key)
    ):
        return REDACTED_FIELD
    if sum(character.isdigit() for character in reduced) > ERROR_FIELD_MAX_DIGITS:
        return REDACTED_FIELD
    return reduced


def created_id(data: object) -> str:
    """The top-level ``id`` of a 2xx body if it is a plain identifier; else ``""``."""
    if not isinstance(data, dict):
        return ""
    value = data.get("id")
    if isinstance(value, str) and _CREATED_ID.match(value):
        return value
    return ""


def _log(method: Method, path: str, result: _Attempt, elapsed_ms: int, object_id: str) -> None:
    status = str(result.status) if result.status is not None else "-"
    line = f"stax method={method.value} path={path} status={status} ms={elapsed_ms}"
    line += f" object_id={object_id or '-'}"
    if result.failure:
        line += f" failure={result.failure}"
    if result.error_fields is not None:
        line += f" error_fields={','.join(result.error_fields) or '-'}"
    logger.info(line)

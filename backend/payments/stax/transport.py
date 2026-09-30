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
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
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


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"


@dataclass(frozen=True, slots=True)
class _Attempt:
    outcome: Outcome
    status: int | None
    response: httpx.Response | None
    failure: str


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
            api_key = self._api_key if self._api_key is not None else settings.STAX_API_KEY
            if not api_key:
                raise ProviderNotPermittedHere("No provider API key is configured")
            self._client = build_client(settings.STAX_API_BASE_URL, api_key, self._http_transport)
        return self._client

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
    ) -> object:
        """Send one logical request; return the decoded JSON body (``None`` if empty).

        ``path`` is a template; ``{id}`` is replaced by the percent-encoded
        ``object_id``. ``idempotent=False`` marks a call that may have taken effect even
        when no response arrived: it is retried after a 429 only (D6).
        """
        if settings.STAX_CALLER != WORKER:
            raise ProviderNotPermittedHere("This process is not permitted to call the provider")
        client = self.client
        url = path.replace(ID_PLACEHOLDER, quote(object_id, safe="")) if object_id else path
        content = mapping.encode_json(json) if json is not None else None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = self._attempt(client, method, url, path, object_id, params, content)
            if result.outcome is Outcome.SUCCESS and result.response is not None:
                try:
                    return mapping.decode_json(result.response.content)
                except ValueError:
                    raise ProviderUnavailable(
                        result.status, "The provider response was not valid JSON"
                    ) from None
            if result.outcome is Outcome.CLIENT_ERROR and result.status is not None:
                raise ProviderRejected(result.status)
            retryable = result.outcome is Outcome.THROTTLED or (
                idempotent and result.outcome in (Outcome.SERVER_ERROR, Outcome.NETWORK_ERROR)
            )
            if not retryable or attempt == MAX_ATTEMPTS:
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
                result = _classify(response)
        except BaseException:
            breaker.abandon()
            raise
        elapsed_ms = self.clock.monotonic_ms() - started
        breaker.record(result.outcome)
        if result.outcome is not Outcome.SUCCESS:
            self.budget.record_http_failure()
        _log(method, path, result, elapsed_ms, object_id)
        return result


def _classify(response: httpx.Response) -> _Attempt:
    status = response.status_code
    if 200 <= status < 300:
        return _Attempt(Outcome.SUCCESS, status, response, "")
    if status == TOO_MANY_REQUESTS:
        return _Attempt(Outcome.THROTTLED, status, response, "ProviderUnavailable")
    if 400 <= status < 500:
        return _Attempt(Outcome.CLIENT_ERROR, status, response, "ProviderRejected")
    # 5xx, and anything else unexpected (an unfollowed redirect, a 1xx).
    return _Attempt(Outcome.SERVER_ERROR, status, response, "ProviderUnavailable")


def _log(method: Method, path: str, result: _Attempt, elapsed_ms: int, object_id: str) -> None:
    status = str(result.status) if result.status is not None else "-"
    line = f"stax method={method.value} path={path} status={status} ms={elapsed_ms}"
    line += f" object_id={object_id or '-'}"
    if result.failure:
        line += f" failure={result.failure}"
    logger.info(line)

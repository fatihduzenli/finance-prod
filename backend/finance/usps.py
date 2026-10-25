"""The USPS address check (docs/qa/PLAN-012f-b-staging-review.md D13).

``check(address)`` asks the USPS Addresses API (v3) for the standardised form of a store's
street, city, state and ZIP. It is advice for staff: the store form saves whatever the
answer, or without one ("Address not verified").

What the USPS documentation states, read 2026-10-25:

- The Addresses 3.0 OpenAPI file, version 3.2.3
  (https://developers.usps.com/sites/default/files/apidoc_specs/addresses-v3r2_4.yaml,
  linked from https://developers.usps.com/addressesv3): servers
  ``https://apis.usps.com/addresses/v3`` and ``https://apis-tem.usps.com/addresses/v3``
  (the test environment); ``GET /address`` with ``streetAddress`` and ``state`` (two
  letters) required, ``city`` and ``ZIPCode`` (five digits) optional but one of them
  needed; a 200 answers ``address`` (``streetAddress``, ``secondaryAddress``, ``city``,
  ``state``, ``ZIPCode``, ``ZIPPlus4``, which may be null) and ``additionalInfo``
  (``DPVConfirmation``: ``Y`` confirmed, ``D`` / ``S`` the primary number only, ``N``
  not confirmed); a 404 is "Address Not Found"; the security scheme is OAuth 2.0 client
  credentials with ``tokenUrl: /oauth2/v3/token`` and the scope ``addresses``.
- The OAuth page (https://developers.usps.com/oauth) renders in the browser only; the
  token request is taken from USPS's own examples
  (https://github.com/USPS/api-examples, README): ``POST
  https://apis.usps.com/oauth2/v3/token`` with ``Content-Type: application/json`` and
  the body ``client_id``, ``client_secret``, ``grant_type: client_credentials``; the
  answer holds ``access_token``, ``token_type: Bearer`` and ``expires_in`` (shown as a
  string). TODO-VERIFY-065: whether the token is granted the ``addresses`` scope with no
  ``scope`` in the request, and whether ``expires_in`` is a number or a string (both are
  read).

Limits: 5 seconds to connect and 5 of silence, no retry, redirects not followed, one
``httpx.Client`` per call. The token is kept in this process until 60 seconds before its
expiry; a 401 on the address call forgets it. Every failure is ``None``; a 404 is
:class:`NoMatch`. Logging: the host, the status (or the error's class name) and the
latency, never the credentials, the token or the address.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

TOKEN_PATH = "/oauth2/v3/token"  # noqa: S105 - a public endpoint path, not a secret
ADDRESS_PATH = "/addresses/v3/address"
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 5.0
TOKEN_MARGIN_SECONDS = 60
#: The longest value sent for each field (the view cuts the form's values to this too).
FIELD_MAX_LENGTH = 100

#: The state codes the API's ``state`` parameter accepts (its pattern in the OpenAPI file).
USPS_STATES: frozenset[str] = frozenset(
    "AA AE AL AK AP AS AZ AR CA CO CT DE DC FM FL GA GU HI ID IL IN IA KS KY LA ME MH MD "
    "MA MI MN MS MO MP MT NE NV NH NJ NM NY NC ND OH OK OR PW PA PR RI SC SD TN TX UT VT "
    "VI VA WA WV WI WY".split()
)
_ZIP5 = re.compile(r"[0-9]{5}")
_ZIP4 = re.compile(r"[0-9]{4}")
_ZIP_INPUT = re.compile(r"\s*([0-9]{5})(?:\s*-?\s*[0-9]{4})?\s*")


@dataclass(frozen=True, slots=True)
class Address:
    """What staff typed: the store's street, city, state code and ZIP."""

    street: str
    city: str
    state: str
    zip: str


@dataclass(frozen=True, slots=True)
class Checked:
    """The standardised address USPS answered; ``zip4`` is empty when it gave none."""

    street: str
    city: str
    state: str
    zip5: str
    zip4: str
    #: ``DPVConfirmation`` is ``Y``: USPS knows the address with its unit, if any.
    dpv_confirmed: bool


@dataclass(frozen=True, slots=True)
class NoMatch:
    """USPS answered that it has no such address (a 404), or the address cannot be asked
    (no state USPS knows, or neither a city nor a ZIP)."""


class _CallFailedError(Exception):
    """Internal: the call did not give a usable answer. Never leaves this module."""


_lock = threading.Lock()
#: (base URL, client ID) -> (token, monotonic expiry). A process-wide cache.
_tokens: dict[tuple[str, str], tuple[str, float]] = {}


def is_configured() -> bool:
    """The check is on when both credentials are set (the guard refuses one alone)."""
    return bool(settings.USPS_CLIENT_ID and settings.USPS_CLIENT_SECRET)


def forget_tokens() -> None:
    """Drop every cached token (a 401, and tests)."""
    with _lock:
        _tokens.clear()


def check(
    address: Address,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Checked | NoMatch | None:
    """The standardised address, :class:`NoMatch`, or ``None`` when the check is off or
    the service did not give a usable answer. ``transport`` and ``clock`` are for tests."""
    if not is_configured():
        return None
    params = _params(address)
    if params is None:
        return NoMatch()
    base = str(settings.USPS_API_BASE_URL).rstrip("/")
    with httpx.Client(
        timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        transport=transport,
        follow_redirects=False,
    ) as client:
        try:
            token = _token(client, base, clock)
            response = _send(client, "GET", base + ADDRESS_PATH, params=params, token=token)
        except _CallFailedError:
            return None
    if response.status_code == 404:
        return NoMatch()
    if response.status_code == 401:
        forget_tokens()
        return None
    if response.status_code != 200:
        return None
    return _checked(response)


def _params(address: Address) -> dict[str, str] | None:
    street = address.street.strip()[:FIELD_MAX_LENGTH]
    city = address.city.strip()[:FIELD_MAX_LENGTH]
    state = address.state.strip().upper()[:2]
    match = _ZIP_INPUT.fullmatch(address.zip[:FIELD_MAX_LENGTH])
    zip5 = match.group(1) if match else ""
    if not street or state not in USPS_STATES or not (city or zip5):
        return None
    params = {"streetAddress": street, "state": state}
    if city:
        params["city"] = city
    if zip5:
        params["ZIPCode"] = zip5
    return params


def _send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    token: str = "",
    **kwargs: Any,
) -> httpx.Response:
    """One request; its host, status and latency logged. A network error is ``_CallFailedError``."""
    host = urlsplit(url).hostname or "?"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    try:
        response = client.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as error:
        elapsed = round((time.monotonic() - started) * 1000)
        logger.warning("USPS %s failed: %s after %d ms", host, type(error).__name__, elapsed)
        raise _CallFailedError from None
    elapsed = round((time.monotonic() - started) * 1000)
    logger.info("USPS %s answered %d in %d ms", host, response.status_code, elapsed)
    return response


def _token(client: httpx.Client, base: str, clock: Callable[[], float]) -> str:
    key = (base, str(settings.USPS_CLIENT_ID))
    with _lock:
        now = clock()
        cached = _tokens.get(key)
        if cached is not None and now < cached[1] - TOKEN_MARGIN_SECONDS:
            return cached[0]
        response = _send(
            client,
            "POST",
            base + TOKEN_PATH,
            json={
                "grant_type": "client_credentials",
                "client_id": settings.USPS_CLIENT_ID,
                "client_secret": settings.USPS_CLIENT_SECRET,
            },
        )
        if response.status_code != 200:
            raise _CallFailedError
        try:
            body = response.json()
            token = body["access_token"]
            lifetime = int(body["expires_in"])
        except Exception:  # an unreadable answer: nothing of it is kept
            raise _CallFailedError from None
        if not isinstance(token, str) or not token or lifetime <= 0:
            raise _CallFailedError
        _tokens[key] = (token, now + lifetime)
        return token


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _checked(response: httpx.Response) -> Checked | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("address"), dict):
        return None
    found = body["address"]
    street = _text(found.get("streetAddress"))
    secondary = _text(found.get("secondaryAddress"))
    city = _text(found.get("city"))
    state = _text(found.get("state"))
    zip5 = _text(found.get("ZIPCode"))
    zip4 = _text(found.get("ZIPPlus4"))
    if not street or not city or state not in USPS_STATES or not _ZIP5.fullmatch(zip5):
        return None
    if zip4 and not _ZIP4.fullmatch(zip4):
        return None
    info = body.get("additionalInfo")
    confirmed = isinstance(info, dict) and info.get("DPVConfirmation") == "Y"
    return Checked(
        street=f"{street} {secondary}" if secondary else street,
        city=city,
        state=state,
        zip5=zip5,
        zip4=zip4,
        dpv_confirmed=confirmed,
    )

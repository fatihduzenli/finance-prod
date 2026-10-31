"""Plaid's API for the Bank deposits module
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md I1).

What Plaid's documentation states, as understood (https://plaid.com/docs/api/; TODO-VERIFY-071
for a real linked business account and the categories of wire, Zelle, ACH credit and check
deposits):

- Hosts: ``https://sandbox.plaid.com``, ``https://development.plaid.com`` and
  ``https://production.plaid.com`` (``PLAID_ENV``). Every call is a ``POST`` of JSON carrying
  ``client_id`` and ``secret`` in the body; an error answers 4xx or 5xx with ``error_type``
  and ``error_code``.
- ``/link/token/create`` (https://plaid.com/docs/api/link/#linktokencreate): ``client_name``,
  ``user.client_user_id``, ``products: ["transactions"]``, ``country_codes: ["US"]``,
  ``language: "en"``; answers ``link_token``.
- ``/item/public_token/exchange`` (https://plaid.com/docs/api/items/#itempublic_tokenexchange):
  ``public_token``; answers ``access_token`` and ``item_id``.
- ``/accounts/get`` (https://plaid.com/docs/api/accounts/#accountsget): ``access_token``;
  answers ``accounts`` (``account_id``, ``mask``, ``type``, ...) and ``item``
  (``institution_name`` where Plaid gives it).
- ``/transactions/sync`` (https://plaid.com/docs/api/products/transactions/#transactionssync):
  ``access_token``, ``cursor`` (left out on the first call), ``count`` (up to 500); answers
  ``added``, ``modified``, ``removed``, ``next_cursor`` and ``has_more``, paged until
  ``has_more`` is false; ``TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`` means start again
  from the first cursor. A transaction's ``amount`` is **positive for money out**: a credit
  has a negative amount. ``date`` is the posted date; ``pending`` ones are not kept (they come
  again posted). Only ``added`` transactions of the linked account are read; a change or a
  removal of one already stored is not applied (a posted credit does not change).

Limits, as :mod:`finance.usps`: one ``httpx.Client`` per call, 10 seconds to connect and 10 of
silence, redirects not followed, one retry on a 5xx (never on a 4xx or a network error), at
most :data:`MAX_PAGES` pages a sync. Logging: the host, the status (or the error's class name)
and the latency only; never a body, the client ID, the secret, a token or an account number.
A failure is a :class:`PlaidError` subclass, whose class name is all the connection keeps.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings

from banking import crypto
from banking.models import BankConnection, Kind, Source
from banking.sources import FetchedTransaction, Linked, LinkRefusedError, SyncResult, record

logger = logging.getLogger(__name__)

HOSTS: dict[str, str] = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}
LINK_TOKEN_PATH = "/link/token/create"  # noqa: S105 - a public endpoint path, not a secret
EXCHANGE_PATH = "/item/public_token/exchange"  # noqa: S105 - a public endpoint path
ACCOUNTS_PATH = "/accounts/get"
SYNC_PATH = "/transactions/sync"
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 10.0
SYNC_PAGE_SIZE = 500
MAX_PAGES = 50
#: Plaid's limit on ``client_name``.
CLIENT_NAME_MAX_LENGTH = 30
MUTATION_DURING_PAGINATION = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
ITEM_LOGIN_REQUIRED = "ITEM_LOGIN_REQUIRED"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MASK = re.compile(r"[0-9A-Za-z]{0,4}")

ACCOUNT_REFUSED = (
    "Choose one checking or savings account of the business in Plaid's window. Nothing was "
    "connected."
)


class PlaidError(Exception):
    """A call to Plaid did not give a usable answer. Never carries a body or a token."""


class PlaidNetworkError(PlaidError):
    """Plaid could not be reached (a timeout, a refused connection)."""


class PlaidServerError(PlaidError):
    """Plaid answered 5xx twice."""


class PlaidRequestError(PlaidError):
    """Plaid refused the request (4xx)."""


class PlaidItemLoginRequiredError(PlaidRequestError):
    """The bank asks for the account to be connected again (``ITEM_LOGIN_REQUIRED``)."""


class PlaidAnswerError(PlaidError):
    """Plaid answered 200 with something unreadable."""


def _base() -> str:
    return HOSTS[str(settings.PLAID_ENV)]


def _credentials() -> dict[str, str]:
    return {"client_id": str(settings.PLAID_CLIENT_ID), "secret": str(settings.PLAID_SECRET)}


class PlaidSource:
    """:class:`~banking.sources.BankTransactionSource` over Plaid's API. ``transport`` is for
    tests (``httpx.MockTransport``)."""

    source = Source.PLAID.value

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    # --- HTTP ---------------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            transport=self._transport,
            follow_redirects=False,
        )

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """One call, retried once on a 5xx; the answer's JSON object, or a :class:`PlaidError`."""
        url = _base() + path
        payload = {**_credentials(), **body}
        with self._client() as client:
            response = _send(client, url, payload)
            if response.status_code >= 500:
                response = _send(client, url, payload)
        if response.status_code >= 500:
            raise PlaidServerError
        if response.status_code >= 400:
            raise _request_error(response)
        if response.status_code != 200:
            raise PlaidAnswerError
        try:
            answer = response.json()
        except ValueError:
            raise PlaidAnswerError from None
        if not isinstance(answer, dict):
            raise PlaidAnswerError
        return answer

    # --- Link ---------------------------------------------------------------------------

    def create_link_token(self, user_reference: str, client_name: str) -> str:
        answer = self._post(
            LINK_TOKEN_PATH,
            {
                "client_name": client_name[:CLIENT_NAME_MAX_LENGTH] or "Bank deposits",
                "user": {"client_user_id": user_reference},
                "products": ["transactions"],
                "country_codes": ["US"],
                "language": "en",
            },
        )
        token = answer.get("link_token")
        if not isinstance(token, str) or not token:
            raise PlaidAnswerError
        return token

    def exchange(self, public_token: str, account_id: str, institution: str) -> Linked:
        answer = self._post(EXCHANGE_PATH, {"public_token": public_token})
        access_token, item_id = answer.get("access_token"), answer.get("item_id")
        if not isinstance(access_token, str) or not access_token:
            raise PlaidAnswerError
        if not isinstance(item_id, str) or not _IDENTIFIER.fullmatch(item_id):
            raise PlaidAnswerError
        accounts = self._post(ACCOUNTS_PATH, {"access_token": access_token})
        account = _chosen_account(accounts.get("accounts"), account_id)
        item = accounts.get("item")
        name = item.get("institution_name") if isinstance(item, dict) else None
        return Linked(
            item_id=item_id,
            account_id=str(account["account_id"]),
            institution=name if isinstance(name, str) and name else institution,
            mask=str(account.get("mask") or ""),
            access_token=access_token,
        )

    # --- Sync ---------------------------------------------------------------------------

    def sync(self, connection: BankConnection) -> SyncResult:
        access_token = crypto.decrypt(str(connection.access_token_encrypted))
        try:
            fetched, cursor, pages = self._pages(access_token, str(connection.cursor))
        except _MutationError:
            fetched, cursor, pages = self._pages(access_token, str(connection.cursor), again=True)
        account = str(connection.account_id)
        mine = [row for row in fetched if row[0] == account]
        return record(
            connection,
            self.source,
            [item for _, item in mine if item is not None],
            cursor=cursor,
            pages=pages,
        )

    def _pages(
        self, access_token: str, cursor: str, *, again: bool = False
    ) -> tuple[list[tuple[str, FetchedTransaction | None]], str, int]:
        found: list[tuple[str, FetchedTransaction | None]] = []
        pages = 0
        while pages < MAX_PAGES:
            body: dict[str, Any] = {"access_token": access_token, "count": SYNC_PAGE_SIZE}
            if cursor:
                body["cursor"] = cursor
            try:
                answer = self._post(SYNC_PATH, body)
            except _MutationError:
                if again:
                    raise PlaidRequestError from None
                raise
            pages += 1
            added = answer.get("added")
            next_cursor = answer.get("next_cursor")
            if not isinstance(added, list) or not isinstance(next_cursor, str):
                raise PlaidAnswerError
            found += [_transaction(row) for row in added if isinstance(row, dict)]
            cursor = next_cursor
            if answer.get("has_more") is not True:
                break
        return found, cursor, pages


class _MutationError(PlaidRequestError):
    """Internal: ``TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION``; the sync starts again."""


def _send(client: httpx.Client, url: str, payload: Mapping[str, Any]) -> httpx.Response:
    """One request; its host, status and latency logged. A network error is
    :class:`PlaidNetworkError`."""
    host = urlsplit(url).hostname or "?"
    started = time.monotonic()
    try:
        response = client.post(url, json=dict(payload), headers={"Accept": "application/json"})
    except httpx.HTTPError as error:
        elapsed = round((time.monotonic() - started) * 1000)
        logger.warning("Plaid %s failed: %s after %d ms", host, type(error).__name__, elapsed)
        raise PlaidNetworkError from None
    elapsed = round((time.monotonic() - started) * 1000)
    logger.info("Plaid %s answered %d in %d ms", host, response.status_code, elapsed)
    return response


def _request_error(response: httpx.Response) -> PlaidRequestError:
    try:
        body = response.json()
    except ValueError:
        body = None
    code = body.get("error_code") if isinstance(body, dict) else None
    if code == MUTATION_DURING_PAGINATION:
        return _MutationError()
    if code == ITEM_LOGIN_REQUIRED:
        return PlaidItemLoginRequiredError()
    return PlaidRequestError()


def _chosen_account(accounts: object, account_id: str) -> dict[str, Any]:
    """The account chosen in Link (its id as the browser gave it), or the item's one
    depository account; :class:`LinkRefusedError` otherwise."""
    if not isinstance(accounts, list):
        raise PlaidAnswerError
    usable = [
        account
        for account in accounts
        if isinstance(account, dict)
        and isinstance(account.get("account_id"), str)
        and _IDENTIFIER.fullmatch(account["account_id"])
        and _MASK.fullmatch(str(account.get("mask") or ""))
        and account.get("type") == "depository"
    ]
    if account_id:
        usable = [account for account in usable if account["account_id"] == account_id]
    if len(usable) != 1:
        raise LinkRefusedError(ACCOUNT_REFUSED)
    return usable[0]


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _counterparty(row: Mapping[str, Any]) -> str:
    parties = row.get("counterparties")
    if isinstance(parties, list):
        for party in parties:
            if isinstance(party, dict) and _text(party.get("name")):
                return _text(party["name"])
    meta = row.get("payment_meta")
    if isinstance(meta, dict) and _text(meta.get("payer")):
        return _text(meta["payer"])
    return _text(row.get("merchant_name"))


def kind_of(row: Mapping[str, Any]) -> str:
    """The deposit's kind from Plaid's fields (TODO-VERIFY-071): a check number is a check;
    the wording or the payment method naming Zelle, a wire or ACH gives those; else other."""
    if row.get("check_number"):
        return Kind.CHECK.value
    meta = row.get("payment_meta")
    method = _text(meta.get("payment_method")).upper() if isinstance(meta, dict) else ""
    words = " ".join(
        (_text(row.get("name")), _text(row.get("original_description")), method)
    ).upper()
    if "ZELLE" in words:
        return Kind.ZELLE.value
    if re.search(r"\b(FED)?WIRE\b", words):
        return Kind.WIRE.value
    if re.search(r"\bACH\b", words):
        return Kind.ACH_CREDIT.value
    if re.search(r"\bCHECK\b|\bCHEQUE\b", words):
        return Kind.CHECK.value
    return Kind.OTHER.value


def _transaction(row: Mapping[str, Any]) -> tuple[str, FetchedTransaction | None]:
    """The row's account and the transaction with money in positive; ``None`` for a pending
    or unreadable row, or one in another currency."""
    account = _text(row.get("account_id"))
    external_id = _text(row.get("transaction_id"))
    if row.get("pending") is True or not _IDENTIFIER.fullmatch(external_id):
        return account, None
    if row.get("iso_currency_code") not in (None, "USD"):
        return account, None
    try:
        amount = -Decimal(str(row.get("amount")))
        posted_on = date.fromisoformat(_text(row.get("date")))
    except (InvalidOperation, ValueError):
        return account, None
    if not amount.is_finite():
        return account, None
    description = _text(row.get("original_description")) or _text(row.get("name"))
    return account, FetchedTransaction(
        external_id=external_id,
        posted_on=posted_on,
        amount=amount.quantize(Decimal("0.01")),
        description=description,
        counterparty=_counterparty(row),
        kind=kind_of(row),
    )

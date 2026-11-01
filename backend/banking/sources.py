"""Where bank transactions come from
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md I1).

A :class:`BankTransactionSource` links the business's bank account (a link token for the
browser, then the exchange of the browser's public token) and syncs its transactions:
``sync(connection)`` reads what is new since the connection's cursor and adds one
:class:`~banking.models.BankTransaction` per credit, idempotently by ``external_id``
(:func:`record`): a second sync of the same transactions adds nothing. Debits are counted and
never kept. Amounts reach :func:`record` with money **in** positive.

- :class:`banking.plaid.client.PlaidSource`: Plaid's API, when ``PLAID_CLIENT_ID``,
  ``PLAID_SECRET`` and ``PLAID_TOKEN_KEY`` are set.
- :class:`FakeSource`: tests and local development; transactions from a fixture
  (``banking/fixtures/fake_transactions.json``, or the list given), no network, no
  credential (its connection stores no token).

:func:`get_source` picks Plaid when the keys are set, the fake when ``APP_ENV`` is local, and
nothing otherwise. Text from the bank is cut to its column, stripped of control characters and
held to the card-number rule (Rule 1): a description or a sender name that holds a number
shaped like a card or an account number is never stored; the row keeps :data:`WITHHELD`
instead and the sync counts it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from banking.models import (
    COUNTERPARTY_MAX_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    BankConnection,
    BankTransaction,
    Kind,
    Source,
)
from ledger.models import MONEY_DECIMAL_PLACES
from ledger.validators import validate_no_payment_data

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_transactions.json"
WITHHELD = "(withheld: the bank's text held a number shaped like a card or account number)"
#: The fake connection's stand-in for a token: the fake holds no credential.
FAKE_TOKEN_MARKER = "fake"  # noqa: S105 - not a credential
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_CENT = Decimal(1).scaleb(-MONEY_DECIMAL_PLACES)
#: The largest amount a money column holds (``numeric(12,2)``).
MONEY_MAX = Decimal("9999999999.99")


@dataclass(frozen=True, slots=True)
class FetchedTransaction:
    """One transaction as a source read it; ``amount`` is positive for money in."""

    external_id: str
    posted_on: date
    amount: Decimal
    description: str
    counterparty: str
    kind: str


@dataclass(frozen=True, slots=True)
class SyncResult:
    added: int = 0
    #: Already stored (the same ``external_id``): nothing added.
    already: int = 0
    debits_ignored: int = 0
    #: Kept with :data:`WITHHELD` in place of the bank's text.
    withheld: int = 0
    pages: int = 0
    #: An amount the ledger cannot hold (more than :data:`MONEY_MAX`): not kept.
    refused: int = 0


@dataclass(frozen=True, slots=True)
class Linked:
    """What the exchange of a public token gives: Plaid's identifiers, the institution and
    the business account's mask, and the access token (never in ``repr``)."""

    item_id: str
    account_id: str
    institution: str
    mask: str
    access_token: str = field(repr=False)


class LinkRefusedError(Exception):
    """The account chosen in Link cannot be used; ``message`` is one sentence for staff."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BankTransactionSource(Protocol):
    #: :class:`~banking.models.Source` value of the rows this source adds.
    source: str

    def sync(self, connection: BankConnection) -> SyncResult:
        """Add the transactions new since ``connection.cursor`` (see the module docstring)."""
        ...

    def create_link_token(self, user_reference: str, client_name: str) -> str:
        """A short-lived link token for the browser's Link."""
        ...

    def exchange(self, public_token: str, account_id: str, institution: str) -> Linked:
        """Exchange the browser's public token for the item; :class:`LinkRefusedError` when
        the chosen account cannot be used."""
        ...


def keys_present() -> bool:
    """Whether the three Plaid secrets are set here (the guard refuses one or two alone)."""
    return bool(settings.PLAID_CLIENT_ID and settings.PLAID_SECRET and settings.PLAID_TOKEN_KEY)


def get_source() -> BankTransactionSource | None:
    """Plaid when the keys are set; the fake when ``APP_ENV`` is local; else ``None``."""
    if keys_present():
        from banking.plaid.client import PlaidSource

        return PlaidSource()
    if settings.APP_ENV == "local":
        return FakeSource()
    return None


# --- Recording ----------------------------------------------------------------------------


def clean_text(raw: object, limit: int) -> tuple[str, bool]:
    """``raw`` as stored: one line, trimmed, cut to ``limit``; and whether it was withheld
    for holding a card or account number."""
    text = _CONTROL.sub(" ", raw if isinstance(raw, str) else "")
    text = " ".join(text.split())[:limit]
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return WITHHELD[:limit], True
    return text, False


def record(
    connection: BankConnection,
    source: str,
    fetched: Sequence[FetchedTransaction],
    *,
    cursor: str,
    pages: int,
) -> SyncResult:
    """Add the credits of ``fetched`` not stored yet and move the connection's cursor, in one
    transaction under the connection's row lock."""
    added = already = debits = withheld = refused = 0
    with transaction.atomic():
        locked = BankConnection.objects.select_for_update().get(pk=connection.pk)
        credits = [item for item in fetched if item.amount > 0]
        debits = len(fetched) - len(credits)
        known = set(
            BankTransaction.objects.filter(
                external_id__in=[item.external_id for item in credits]
            ).values_list("external_id", flat=True)
        )
        for item in credits:
            if item.amount > MONEY_MAX:
                refused += 1
                continue
            if item.external_id in known:
                already += 1
                continue
            description, hidden = clean_text(item.description, DESCRIPTION_MAX_LENGTH)
            counterparty, hidden_too = clean_text(item.counterparty, COUNTERPARTY_MAX_LENGTH)
            withheld += int(hidden or hidden_too)
            BankTransaction.objects.create(
                source=source,
                external_id=item.external_id,
                posted_on=item.posted_on,
                amount=item.amount,
                description=description,
                counterparty=counterparty,
                kind=item.kind if item.kind in Kind.values else Kind.OTHER,
            )
            known.add(item.external_id)
            added += 1
        locked.cursor = cursor
        locked.last_sync_at = timezone.now()
        locked.last_error = ""
        locked.save(update_fields=["cursor", "last_sync_at", "last_error"])
    connection.cursor = cursor
    return SyncResult(added, already, debits, withheld, pages, refused)


# --- The fake -----------------------------------------------------------------------------


def _amount(raw: object) -> Decimal | None:
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount != amount.quantize(_CENT):
        return None
    return amount


def _fake_rows(raw: Sequence[dict[str, Any]]) -> list[FetchedTransaction]:
    found = []
    for row in raw:
        amount = _amount(row.get("amount"))
        if amount is None or not row.get("external_id"):
            continue
        found.append(
            FetchedTransaction(
                external_id=str(row["external_id"]),
                posted_on=date.fromisoformat(str(row["posted_on"])),
                amount=amount,
                description=str(row.get("description", "")),
                counterparty=str(row.get("counterparty", "")),
                kind=str(row.get("kind", Kind.OTHER)),
            )
        )
    return found


class FakeSource:
    """Transactions from a fixture (money in positive, debits negative); no network."""

    source = Source.FAKE.value

    def __init__(self, transactions: Sequence[dict[str, Any]] | None = None) -> None:
        self._transactions = transactions

    def _rows(self) -> list[FetchedTransaction]:
        if self._transactions is not None:
            return _fake_rows(self._transactions)
        loaded = json.loads(FIXTURE.read_text(encoding="utf-8"))
        return _fake_rows(loaded if isinstance(loaded, list) else [])

    def sync(self, connection: BankConnection) -> SyncResult:
        rows = self._rows()
        return record(connection, self.source, rows, cursor=f"fake-{len(rows)}", pages=1)

    def create_link_token(self, user_reference: str, client_name: str) -> str:
        return "link-fake-local"

    def exchange(self, public_token: str, account_id: str, institution: str) -> Linked:
        return Linked(
            item_id="fake-item",
            account_id="fake-account",
            institution="Fake Bank",
            mask="0000",
            access_token=FAKE_TOKEN_MARKER,
        )

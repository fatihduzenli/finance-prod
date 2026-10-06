"""The contact log of an operator (docs/qa/PLAN-011b D7).

Billing records each call, e-mail, visit and promise to pay as a :class:`ContactEntry`,
dated in New York days. Nothing edits or deletes an entry. An entry may close an open
promise of the same operator: the promise's ``closed_by`` then names the new entry. Each
entry writes an audit row (``contact_entry.created``; and ``contact_entry.promise_closed``
on the promise it closes).

**When a promise is broken** (:func:`broken_promises`), on ``today`` (a New York date),
a promise is broken when all of these hold:

1. it is not closed (``closed_by`` is null);
2. its ``promised_date`` is before ``today``;
3. its operator still has an open balance on ``today``: at least one open charge billed
   to it, as :func:`ledger.services.receivables.open_balances` decides (rent, insurance or
   one-off, deposits left out), due on any date;
4. no payment of the operator settled on or after the ``promised_date``: no row of
   ``payments`` with status ``settled``, not a refund, and ``settled_on`` on or after that
   date, whose charge or invoice is billed to the operator.

A broken promise is an attention item (:mod:`ledger.services.attention`) until a later
entry closes it or such a payment settles. The reads take a fixed number of queries
whatever the number of promises.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, QuerySet
from django.db.models.functions import Coalesce

from ledger.models import (
    AuditLog,
    ContactEntry,
    ContactKind,
    Operator,
    Payment,
    PaymentStatus,
)
from ledger.services import receivables
from ledger.services.csv_export import CsvWriter
from ledger.validators import contains_bank_routing_pattern, contains_card_number, visible_text

ENTITY_CONTACT = "contact_entry"
#: The most entries the operator page lists; the CSV holds every one.
PAGE_LIMIT = 20
#: Largest promised amount accepted (the money column's range).
AMOUNT_MAX = Decimal("9999999999.99")

BODY_REQUIRED = "A note is required: say what was said or agreed."
BODY_PAYMENT_DATA = "This text may not contain card or account numbers."
PROMISE_FIELDS_REQUIRED = "A promise to pay needs the promised date and amount."
PROMISE_FIELDS_ONLY = "Only a promise to pay has a promised date and amount."
AMOUNT_POSITIVE = "The promised amount must be more than zero."
AMOUNT_TOO_LARGE = "The promised amount is too large."
CLOSES_UNKNOWN = "That promise is not an open promise of this operator."
ARCHIVED = "This operator is archived; no contact can be added."
ADDED = "Contact entry added."

CSV_HEADER: tuple[str, ...] = (
    "Date",
    "Kind",
    "Note",
    "Promised date",
    "Promised amount (USD)",
    "Promise closed",
    "Recorded by",
    "Recorded at",
)


@dataclass(frozen=True, slots=True)
class ContactInput:
    kind: str
    happened_on: date
    body: str
    promised_date: date | None = None
    promised_amount: Decimal | None = None
    #: An open promise of the operator that this entry closes.
    closes: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AddOutcome:
    ok: bool
    message: str
    #: The form field the message belongs to, or ``None``.
    field: str | None = None
    entry: ContactEntry | None = None


def entries(operator: Operator) -> QuerySet[ContactEntry]:
    """The operator's entries, newest first (by day, then by when they were recorded)."""
    return (
        ContactEntry.objects.filter(operator=operator)
        .select_related("author", "closed_by")
        .order_by("-happened_on", "-created_at", "-id")
    )


def open_promises(operator: Operator) -> QuerySet[ContactEntry]:
    """The operator's promises not closed yet, newest first: what a new entry may close."""
    return entries(operator).filter(kind=ContactKind.PROMISE, closed_by__isnull=True)


def _refusal(data: ContactInput, body: str) -> tuple[str, str] | None:
    if not body:
        return BODY_REQUIRED, "body"
    if contains_card_number(body) or contains_bank_routing_pattern(body):
        return BODY_PAYMENT_DATA, "body"
    promise = data.kind == ContactKind.PROMISE
    if promise and (data.promised_date is None or data.promised_amount is None):
        field = "promised_date" if data.promised_date is None else "promised_amount"
        return PROMISE_FIELDS_REQUIRED, field
    if not promise and (data.promised_date is not None or data.promised_amount is not None):
        return PROMISE_FIELDS_ONLY, "promised_date"
    if data.promised_amount is not None:
        if data.promised_amount <= 0:
            return AMOUNT_POSITIVE, "promised_amount"
        if data.promised_amount > AMOUNT_MAX:
            return AMOUNT_TOO_LARGE, "promised_amount"
    return None


def _state(entry: ContactEntry) -> dict[str, object]:
    return {
        "operator": str(entry.operator_id),
        "kind": entry.kind,
        "happened_on": entry.happened_on.isoformat(),
        "promised_date": entry.promised_date.isoformat() if entry.promised_date else None,
        "promised_amount": (
            str(entry.promised_amount) if entry.promised_amount is not None else None
        ),
        "body": entry.body,
    }


def add_entry(operator_id: uuid.UUID, data: ContactInput, *, actor: User | None) -> AddOutcome:
    """Record one entry; close the named promise when there is one. Refusals change
    nothing and name the field. The body is stored as :func:`visible_text`."""
    if data.kind not in ContactKind.values:
        return AddOutcome(False, "Choose what kind of contact this was.", "kind")
    body = visible_text(data.body or "")
    refusal = _refusal(data, body)
    if refusal is not None:
        return AddOutcome(False, refusal[0], refusal[1])
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        if operator.archived_at is not None:
            return AddOutcome(False, ARCHIVED)
        closing: ContactEntry | None = None
        if data.closes is not None:
            closing = (
                ContactEntry.objects.select_for_update()
                .filter(
                    pk=data.closes,
                    operator=operator,
                    kind=ContactKind.PROMISE,
                    closed_by__isnull=True,
                )
                .first()
            )
            if closing is None:
                return AddOutcome(False, CLOSES_UNKNOWN, "closes")
        entry = ContactEntry(
            operator=operator,
            kind=data.kind,
            happened_on=data.happened_on,
            promised_date=data.promised_date,
            promised_amount=data.promised_amount,
            body=body,
            author=actor,
        )
        try:
            entry.save()
        except ValidationError:
            return AddOutcome(False, BODY_PAYMENT_DATA, "body")
        _audit(actor, "contact_entry.created", entry.pk, None, _state(entry))
        if closing is not None:
            closing.closed_by = entry
            closing.save(update_fields=["closed_by"])
            _audit(
                actor,
                "contact_entry.promise_closed",
                closing.pk,
                {"closed_by": None},
                {"closed_by": str(entry.pk)},
            )
    return AddOutcome(True, ADDED, entry=entry)


def _audit(
    actor: User | None,
    action: str,
    entry_id: uuid.UUID,
    before: dict[str, object] | None,
    after: dict[str, object],
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_CONTACT,
        entity_id=str(entry_id),
        before=before,
        after=after,
    )


# --- Broken promises (the module docstring's rule) -----------------------------------------


def _last_settled(operators: Iterable[uuid.UUID], since: date) -> dict[uuid.UUID, date]:
    """Operator -> the latest ``settled_on`` of its settled, non-refund payments on or
    after ``since``. One query."""
    billed = Coalesce(
        "invoice__operator",
        "rent_charge__lease__operator",
        "insurance_charge__lease__operator",
        "one_off_charge__operator",
        "one_off_charge__lease__operator",
    )
    rows = (
        Payment.objects.filter(status=PaymentStatus.SETTLED, is_refund=False, settled_on__gte=since)
        .annotate(billed=billed)
        .filter(billed__in=list(operators))
        .values("billed")
        .annotate(last=Max("settled_on"))
        .order_by()
    )
    return {row["billed"]: row["last"] for row in rows}


def broken_promises(today: date, operator: uuid.UUID | None = None) -> list[ContactEntry]:
    """The promises broken on ``today`` (see the module docstring), oldest promised date
    first; with ``operator``, only that operator's. A fixed number of queries."""
    promises = list(
        ContactEntry.objects.filter(
            kind=ContactKind.PROMISE, closed_by__isnull=True, promised_date__lt=today
        )
        .filter(**({"operator": operator} if operator is not None else {}))
        .select_related("operator")
        .order_by("promised_date", "created_at", "id")
    )
    if not promises:
        return []
    owing = {
        row.operator_id
        for row in receivables.open_balances(today, operator=operator)
        if row.operator_id is not None
    }
    candidates = [promise for promise in promises if promise.operator_id in owing]
    if not candidates:
        return []
    since = min(promise.promised_date for promise in candidates if promise.promised_date)
    settled = _last_settled({promise.operator_id for promise in candidates}, since)
    return [
        promise
        for promise in candidates
        if promise.promised_date is not None
        and not (
            promise.operator_id in settled and settled[promise.operator_id] >= promise.promised_date
        )
    ]


# --- CSV ---------------------------------------------------------------------------------


def csv_lines(operator: Operator) -> Iterator[str]:
    """Every entry of the operator, newest first, with readable headers."""
    records = (
        [
            entry.happened_on,
            entry.get_kind_display(),
            entry.body,
            entry.promised_date,
            entry.promised_amount,
            entry.closed_by_id is not None if entry.kind == ContactKind.PROMISE else None,
            entry.author.get_username() if entry.author is not None else "",
            entry.created_at,
        ]
        for entry in entries(operator).iterator(chunk_size=500)
    )
    return CsvWriter(CSV_HEADER).lines(records)

"""The payments ledger: every payment, filtered, totalled and exported (PLAN-009 D4, D9).

Read only: nothing here writes or calls the payment provider. Filters come from the
query string and an unusable value is ignored, never an error:

- ``date_by`` ``paid`` (the settled date, the default) or ``initiated``, with
  ``date_from`` and ``date_to`` (ISO dates, both inclusive);
- ``method``: a payment method;
- ``operator`` and ``store``: IDs, matched through the payment's charge (rent or
  insurance) or, for the operator, its invoice;
- ``status``: ``pending``, ``settled``, ``returned`` or ``voided`` (payments that are not
  refunds), or ``refunded`` (the refund rows).

The totals of the filtered set keep ``settled`` (collected), ``pending``, ``returned``
and ``refunded`` apart: a pending payment is never counted as collected.

The CSV export (:func:`csv_lines`) has one header row and one row per payment; the
``csv`` module quotes commas, quotes and line breaks, so a note never splits a row.
Amounts are plain decimals with two places, dates ISO 8601. The provider's
transaction ID is masked exactly as on screen. A text cell that a spreadsheet would
read as a formula is prefixed with an apostrophe.
"""

from __future__ import annotations

import csv
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Exists, OuterRef, Q, QuerySet, Sum
from django.db.models.functions import Coalesce

from ledger.models import (
    ZERO,
    ChargeNote,
    Operator,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Store,
)
from payments.models import ExternalRef
from payments.operator_sync import PROVIDER_KEY
from payments.provider import mask_identifier
from payments.webhooks.handlers.common import ENTITY_PAYMENT, KIND_TRANSACTION

CENT = Decimal("0.01")
DATE_BY_PAID = "paid"
DATE_BY_INITIATED = "initiated"
DATE_BY_CHOICES: tuple[tuple[str, str], ...] = (
    (DATE_BY_PAID, "Paid on"),
    (DATE_BY_INITIATED, "Initiated on"),
)
STATUS_REFUNDED = "refunded"
STATUS_CHOICES: tuple[tuple[str, str], ...] = (
    *((value, str(label)) for value, label in PaymentStatus.choices),
    (STATUS_REFUNDED, "Refunded (refund rows)"),
)
#: Rows read from the database at a time by the export.
EXPORT_CHUNK = 500
CSV_HEADER: tuple[str, ...] = (
    "payment_id",
    "paid_on",
    "initiated_on",
    "operator",
    "store",
    "charge_kind",
    "charge_period",
    "charge_debit",
    "policy_year",
    "method",
    "status",
    "is_refund",
    "refunds_payment_id",
    "amount",
    "reference",
    "note",
    "charge_has_note",
    "provider_transaction",
    "return_code",
    "recorded_by",
)
#: First characters that make a spreadsheet read a cell as a formula.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class PaymentFilter:
    date_by: str = DATE_BY_PAID
    date_from: date | None = None
    date_to: date | None = None
    method: str = ""
    operator: uuid.UUID | None = None
    store: uuid.UUID | None = None
    status: str = ""

    def as_query(self) -> dict[str, str]:
        """The filter as query-string values, empty ones left out."""
        values = {
            "date_by": self.date_by if self.date_by != DATE_BY_PAID else "",
            "date_from": self.date_from.isoformat() if self.date_from else "",
            "date_to": self.date_to.isoformat() if self.date_to else "",
            "method": self.method,
            "operator": str(self.operator) if self.operator else "",
            "store": str(self.store) if self.store else "",
            "status": self.status,
        }
        return {name: value for name, value in values.items() if value}


@dataclass(frozen=True, slots=True)
class Totals:
    settled: Decimal
    pending: Decimal
    returned: Decimal
    refunded: Decimal
    count: int


@dataclass(frozen=True, slots=True)
class LedgerRow:
    payment: Payment
    operator: Operator | None
    store: Store | None
    #: The provider's transaction ID, masked; empty when there is none.
    transaction: str
    has_note: bool


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value or "")
    except ValueError:
        return None


def parse_filter(params: Mapping[str, str]) -> PaymentFilter:
    """A filter from query-string values; anything unusable is dropped."""
    date_by = params.get("date_by", "")
    method = params.get("method", "")
    status = params.get("status", "")
    return PaymentFilter(
        date_by=date_by if date_by == DATE_BY_INITIATED else DATE_BY_PAID,
        date_from=_date(params.get("date_from")),
        date_to=_date(params.get("date_to")),
        method=method if method in PaymentMethod.values else "",
        operator=_uuid(params.get("operator")),
        store=_uuid(params.get("store")),
        status=status if status in dict(STATUS_CHOICES) else "",
    )


def ledger_payments(selected: PaymentFilter) -> QuerySet[Payment]:
    """The filtered payments, latest first, with what the rows need already joined."""
    rows = Payment.objects.select_related(
        "invoice__operator",
        "rent_charge__lease__store",
        "rent_charge__lease__operator",
        "insurance_charge__lease__store",
        "insurance_charge__lease__operator",
        "recorded_by",
    ).annotate(
        on=Coalesce("settled_on", "initiated_on"),
        has_note=Exists(ChargeNote.objects.filter(rent_charge=OuterRef("rent_charge")))
        | Exists(ChargeNote.objects.filter(insurance_charge=OuterRef("insurance_charge"))),
    )
    column = "initiated_on" if selected.date_by == DATE_BY_INITIATED else "settled_on"
    if selected.date_from is not None:
        rows = rows.filter(**{f"{column}__gte": selected.date_from})
    if selected.date_to is not None:
        rows = rows.filter(**{f"{column}__lte": selected.date_to})
    if selected.method:
        rows = rows.filter(method=selected.method)
    if selected.operator is not None:
        rows = rows.filter(
            Q(rent_charge__lease__operator_id=selected.operator)
            | Q(insurance_charge__lease__operator_id=selected.operator)
            | Q(invoice__operator_id=selected.operator)
        )
    if selected.store is not None:
        rows = rows.filter(
            Q(rent_charge__lease__store_id=selected.store)
            | Q(insurance_charge__lease__store_id=selected.store)
        )
    if selected.status == STATUS_REFUNDED:
        rows = rows.filter(is_refund=True)
    elif selected.status:
        rows = rows.filter(is_refund=False, status=selected.status)
    return rows.order_by("-on", "-created_at", "-id")


def totals(rows: QuerySet[Payment]) -> Totals:
    """Settled (collected), pending, returned and refunded totals, kept apart.

    A refund counts unless it was voided; the other three count payments that are not
    refunds, by status.
    """
    payment = Q(is_refund=False)
    sums = rows.order_by().aggregate(
        settled=Sum("amount", filter=payment & Q(status=PaymentStatus.SETTLED)),
        pending=Sum("amount", filter=payment & Q(status=PaymentStatus.PENDING)),
        returned=Sum("amount", filter=payment & Q(status=PaymentStatus.RETURNED)),
        refunded=Sum("amount", filter=Q(is_refund=True) & ~Q(status=PaymentStatus.VOIDED)),
    )

    def money(name: str) -> Decimal:
        return Decimal(sums[name] or ZERO).quantize(CENT)

    return Totals(
        settled=money("settled"),
        pending=money("pending"),
        returned=money("returned"),
        refunded=money("refunded"),
        count=rows.count(),
    )


def _transactions(payment_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    return dict(
        ExternalRef.objects.filter(
            entity_type=ENTITY_PAYMENT,
            entity_id__in=list(payment_ids),
            provider=PROVIDER_KEY,
            external_kind=KIND_TRANSACTION,
            superseded_at__isnull=True,
        ).values_list("entity_id", "external_id")
    )


def rows_for(payments: Iterable[Payment]) -> list[LedgerRow]:
    """What the ledger shows of each payment (one query for the transaction IDs)."""
    items = list(payments)
    refs = _transactions(item.pk for item in items)
    result = []
    for item in items:
        lease = (
            item.rent_charge.lease
            if item.rent_charge is not None
            else item.insurance_charge.lease
            if item.insurance_charge is not None
            else None
        )
        operator = lease.operator if lease is not None else None
        if operator is None and item.invoice is not None:
            operator = item.invoice.operator
        ref = refs.get(item.pk, "")
        result.append(
            LedgerRow(
                payment=item,
                operator=operator,
                store=lease.store if lease is not None else None,
                transaction=mask_identifier(ref) if ref else "",
                has_note=bool(getattr(item, "has_note", False)),
            )
        )
    return result


def _text(value: str) -> str:
    """A text cell; one a spreadsheet would read as a formula starts with an apostrophe."""
    return f"'{value}" if value.startswith(_FORMULA_START) else value


def _iso(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def _money(value: Decimal) -> str:
    return format(Decimal(value).quantize(CENT), "f")


def csv_record(row: LedgerRow) -> list[str]:
    payment = row.payment
    charge = payment.rent_charge
    return [
        str(payment.pk),
        _iso(payment.settled_on),
        _iso(payment.initiated_on),
        _text(row.operator.display_name if row.operator is not None else ""),
        _text(row.store.external_label if row.store is not None else ""),
        payment.kind,
        f"{charge.period_month:%Y-%m}" if charge is not None else "",
        str(charge.seq) if charge is not None else "",
        str(payment.policy_year) if payment.policy_year is not None else "",
        payment.method,
        payment.status,
        "yes" if payment.is_refund else "no",
        str(payment.parent_payment_id) if payment.parent_payment_id else "",
        _money(payment.amount),
        _text(payment.reference),
        _text(payment.note),
        "yes" if row.has_note else "no",
        row.transaction,
        _text(payment.return_code),
        _text(payment.recorded_by.get_username() if payment.recorded_by is not None else ""),
    ]


class _Line:
    """A file-like object whose ``write`` returns the line, for a streamed ``csv.writer``."""

    def write(self, value: str) -> str:
        return value


def csv_lines(selected: PaymentFilter) -> Iterator[str]:
    """The export of :func:`ledger_payments`, one CSV line at a time."""
    writer = csv.writer(_Line())
    yield writer.writerow(CSV_HEADER)
    chunk: list[Payment] = []
    for payment in ledger_payments(selected).iterator(chunk_size=EXPORT_CHUNK):
        chunk.append(payment)
        if len(chunk) == EXPORT_CHUNK:
            yield from (writer.writerow(csv_record(row)) for row in rows_for(chunk))
            chunk = []
    yield from (writer.writerow(csv_record(row)) for row in rows_for(chunk))

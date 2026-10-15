"""The payments ledger: every payment, filtered, totalled and exported (PLAN-009 D4, D9).

Read only: nothing here writes or calls the payment provider. Filters come from the
query string and an unusable value is ignored, never an error:

- ``date_by`` ``paid`` (the settled date, the default) or ``initiated``, with
  ``date_from`` and ``date_to`` (ISO dates, both inclusive);
- ``method``: a payment method;
- ``operator`` and ``store``: IDs, matched through the payment's charge (rent,
  insurance or one-off) or, for the operator, its invoice;
- ``status``: ``pending``, ``settled``, ``returned`` or ``voided`` (payments that are not
  refunds), or ``refunded`` (the refund rows).

The totals of the filtered set keep ``settled`` (collected), ``pending``, ``returned``
and ``refunded`` apart: a pending payment is never counted as collected. Deposits are
not revenue (PLAN-010b D7): payments on a setup charge of a refundable fee type, their
refunds and deposit refunds (``refund_deposit``) are left out of those four and shown
on their own line, ``deposits_held``. What a resolution releases is no longer held
(:func:`deposits_released`, the rule the P&L footer uses too): a deposit transferred to
another lease of the operator is paid in again on that lease, so the transfer is
subtracted from the deposit it moved (held once, not twice); and a withheld amount
(a whole withheld deposit, or the remainder kept from a partial refund or transfer)
became revenue the day it was withheld, so it is not held either.

The ledger's ``deposits_held`` is the total of the filtered set, not a balance at a
date: a date range keeps the deposit payments in that range, and subtracts every
release of those deposits whatever its date (a release after ``date_to`` included), so
it can differ from the P&L footer, which counts up to the end of its range. (A known
limitation, deferred.)

A payment on an installment (PLAN-012e D12) is listed as any one-off payment and names
the charge its plan splits (``Installment of`` in the export, a link on the page).

The CSV export (:func:`csv_lines`) has one header row and one row per payment, written
by :class:`ledger.services.csv_export.CsvWriter` (plain-English headers, amounts as plain
decimals with two places, dates ISO 8601, formula-safe text, at most
``CSV_MAX_ROWS`` rows). The provider's transaction ID is masked exactly as on screen.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Exists, F, OuterRef, Q, QuerySet, Sum
from django.db.models.functions import Coalesce

from ledger.models import (
    ZERO,
    ChargeNote,
    DepositResolution,
    DepositResolutionKind,
    InvoiceLineItem,
    Lease,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
    Store,
)
from ledger.services import installments
from ledger.services.csv_export import CsvWriter
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
    "Payment ID",
    "Paid date",
    "Initiated date",
    "Operator",
    "Store",
    "Charge kind",
    "Rent month",
    "Debit of the month",
    "Policy year",
    "Method",
    "Status",
    "Refund",
    "Refund of payment ID",
    "Amount (USD)",
    "Reference",
    "Note",
    "Charge has a note",
    "Provider transaction (masked)",
    "Return code",
    "Recorded by",
    "Installment of",
)


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
    #: Settled deposit payments minus their settled refunds, the deposit refunds and
    #: what their resolutions released (transferred or withheld).
    deposits_held: Decimal = ZERO


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
        "one_off_charge__fee_type",
        "one_off_charge__lease__store",
        "one_off_charge__lease__operator",
        "one_off_charge__operator",
        "one_off_charge__plan__rent_charge",
        "one_off_charge__plan__one_off_charge__fee_type",
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
            | Q(one_off_charge__operator_id=selected.operator)
            | Q(one_off_charge__lease__operator_id=selected.operator)
            | Q(invoice__operator_id=selected.operator)
        )
    if selected.store is not None:
        rows = rows.filter(
            Q(rent_charge__lease__store_id=selected.store)
            | Q(insurance_charge__lease__store_id=selected.store)
            | Q(one_off_charge__lease__store_id=selected.store)
        )
    if selected.status == STATUS_REFUNDED:
        rows = rows.filter(is_refund=True)
    elif selected.status:
        rows = rows.filter(is_refund=False, status=selected.status)
    return rows.order_by("-on", "-created_at", "-id")


def multi_line_charges() -> Any:
    """The first lines' charges of the invoice builder's multi-line invoices (PLAN-012
    D6), a Rent line counted as a line (PLAN-012b-b D5), as a subquery: a payment on one
    pays the invoice's lines in line order."""
    return InvoiceLineItem.objects.filter(
        Q(one_off_charge__isnull=False) | Q(rent_charge__isnull=False),
        position__gt=1,
        invoice__one_off_charge__isnull=False,
    ).values("invoice__one_off_charge")


def on_multi_line_invoice() -> Q:
    """A settled one-off payment (or refund) on a multi-line invoice's first line: its
    amount is split by :func:`invoice_line_parts`, never counted by its own charge."""
    return Q(
        kind=PaymentKind.ONE_OFF,
        status=PaymentStatus.SETTLED,
        one_off_charge__in=multi_line_charges(),
    )


#: The category of a Rent line's part (PLAN-012b-b D7): rent collected.
RENT_LINE = "rent"


@dataclass(frozen=True, slots=True)
class LinePart:
    """The part of a payment that paid one line of a multi-line invoice (negative for
    a refund: taken back from that line). A Rent line's part has the category
    :data:`RENT_LINE`, its rent charge's id and that charge's store and operator (the
    lease the line bills, which may be another store of the operator)."""

    charge_id: uuid.UUID
    share: Decimal
    category: str
    refundable: bool
    store_id: uuid.UUID | None = None
    operator_id: uuid.UUID | None = None


def _line_share(paid: Decimal, before: Decimal, price: Decimal) -> Decimal:
    """What ``paid`` (in total, on the invoice) pays of a line whose earlier lines bill
    ``before``: between zero and its ``price``."""
    return min(max(paid - before, ZERO), price)


def invoice_line_parts(payments: QuerySet[Payment]) -> dict[uuid.UUID, list[LinePart]]:
    """Per payment of ``payments`` that is :func:`on_multi_line_invoice`, how it paid the
    invoice's lines (PLAN-012 D6): the first line's charge first, then the others in line
    order (a Rent line's rent charge among them, PLAN-012b-b D5, unless waived), refunds
    taken back from the last line first, the lines of waived or voided charges left out,
    what is paid beyond every line counted with the first line. The split of a payment
    depends on the settled payments before it on the same invoice, so every settled
    payment on those invoices is read in settled order (4 queries). The parts of one
    payment sum to its signed amount."""
    wanted = set(payments.filter(on_multi_line_invoice()).values_list("pk", flat=True))
    if not wanted:
        return {}
    charges = Payment.objects.filter(pk__in=wanted).values("one_off_charge")
    ordered: dict[uuid.UUID, list[tuple[bool, int, LinePart]]] = {}
    for line in (
        InvoiceLineItem.objects.filter(
            invoice__one_off_charge__in=charges, one_off_charge__isnull=False
        )
        .exclude(one_off_charge__status__in=[OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED])
        .values(
            "one_off_charge",
            "position",
            first=F("invoice__one_off_charge"),
            price=F("one_off_charge__price"),
            category=F("one_off_charge__category"),
            refundable=F("one_off_charge__fee_type__refundable"),
        )
    ):
        ordered.setdefault(line["first"], []).append(
            (
                line["one_off_charge"] != line["first"],
                line["position"],
                LinePart(
                    line["one_off_charge"],
                    Decimal(line["price"] or ZERO),
                    str(line["category"]),
                    line["refundable"] is True,
                ),
            )
        )
    for rent in (
        InvoiceLineItem.objects.filter(
            invoice__one_off_charge__in=charges, rent_charge__isnull=False
        )
        .exclude(rent_charge__status=RentChargeStatus.WAIVED)
        .values(
            "rent_charge",
            "position",
            first=F("invoice__one_off_charge"),
            price=F("rent_charge__amount_due"),
            store=F("rent_charge__lease__store"),
            operator=F("rent_charge__lease__operator"),
        )
    ):
        ordered.setdefault(rent["first"], []).append(
            (
                True,
                rent["position"],
                LinePart(
                    rent["rent_charge"],
                    Decimal(rent["price"] or ZERO),
                    RENT_LINE,
                    False,
                    rent["store"],
                    rent["operator"],
                ),
            )
        )
    # Each line's part carries its price in ``share`` until the split below.
    lines = {
        first: [part for _, _, part in sorted(rows, key=lambda row: (row[0], row[1]))]
        for first, rows in ordered.items()
    }
    paid: dict[uuid.UUID, Decimal] = {}
    parts: dict[uuid.UUID, list[LinePart]] = {}
    for row in (
        Payment.objects.filter(on_multi_line_invoice(), one_off_charge__in=charges)
        .values("pk", "amount", "is_refund", charge=F("one_off_charge"))
        .order_by("settled_on", "created_at", "id")
    ):
        amount = Decimal(row["amount"])
        signed = -amount if row["is_refund"] else amount
        before = paid.get(row["charge"], ZERO)
        after = before + signed
        paid[row["charge"]] = after
        if row["pk"] not in wanted:
            continue
        billed = ZERO
        split: list[LinePart] = []
        for line_part in lines.get(row["charge"], []):
            price = line_part.share
            share = _line_share(after, billed, price) - _line_share(before, billed, price)
            split.append(replace(line_part, share=share))
            billed += price
        if split:
            beyond = signed - sum((part.share for part in split), ZERO)
            split[0] = replace(split[0], share=split[0].share + beyond)
        parts[row["pk"]] = split
    return parts


def totals(rows: QuerySet[Payment]) -> Totals:
    """Settled (collected), pending, returned and refunded totals, kept apart, and the
    deposits held.

    A refund counts unless it was voided; the other three count payments that are not
    refunds, by status. None of the four counts a deposit row (PLAN-010b D7). The
    deposits held subtract what the resolutions of the deposits paid in the set
    released (:func:`deposits_released`), whatever the resolution's date. A settled
    payment on a multi-line invoice counts by the lines it paid
    (:func:`invoice_line_parts`): its part on a deposit line is held, the rest collected
    (refunded, for a refund).
    """
    deposit = Q(one_off_charge__fee_type__refundable=True) | Q(kind=PaymentKind.REFUND_DEPOSIT)
    split = on_multi_line_invoice()
    payment = Q(is_refund=False) & ~deposit
    settled = Q(status=PaymentStatus.SETTLED)
    held_in = deposit & Q(kind=PaymentKind.ONE_OFF, is_refund=False) & settled & ~split
    held_out = deposit & (Q(kind=PaymentKind.REFUND_DEPOSIT) | Q(is_refund=True)) & settled & ~split
    sums = rows.order_by().aggregate(
        settled=Sum("amount", filter=payment & settled & ~split),
        pending=Sum("amount", filter=payment & Q(status=PaymentStatus.PENDING)),
        returned=Sum("amount", filter=payment & Q(status=PaymentStatus.RETURNED)),
        refunded=Sum(
            "amount",
            filter=Q(is_refund=True) & ~deposit & ~Q(status=PaymentStatus.VOIDED) & ~split,
        ),
        held_in=Sum("amount", filter=held_in),
        held_out=Sum("amount", filter=held_out),
    )

    def money(name: str) -> Decimal:
        return Decimal(sums[name] or ZERO).quantize(CENT)

    collected, refunded, held = money("settled"), money("refunded"), ZERO
    held_charges: set[uuid.UUID] = set()
    for payment_parts in invoice_line_parts(rows).values():
        for part in payment_parts:
            if part.refundable:
                held += part.share
                if part.share > 0:
                    held_charges.add(part.charge_id)
            elif part.share < 0:
                refunded -= part.share
            else:
                collected += part.share
    released = deposits_released(
        DepositResolution.objects.filter(
            Q(setup_charge__in=rows.filter(held_in).order_by().values("one_off_charge_id"))
            | Q(setup_charge__in=held_charges)
        )
    )
    return Totals(
        settled=collected,
        pending=money("pending"),
        returned=money("returned"),
        refunded=refunded,
        count=rows.count(),
        deposits_held=money("held_in") - money("held_out") + held - released,
    )


def deposits_released(resolutions: QuerySet[DepositResolution]) -> Decimal:
    """What ``resolutions`` released from the deposits held, other than by a payment.

    Two parts, and whatever counts deposits held subtracts both (the payments ledger
    and the P&L footer both use this):

    - a transfer's ``amount``: the deposit is paid in again on the receiving lease (a
      ``one_off`` payment), so it is counted there, once;
    - every ``withheld_amount``: the whole deposit when withheld, the remainder kept
      from a partial refund or transfer. Withheld money is revenue from the day it is
      withheld (PLAN-010b D7), not a deposit held.

    A refund's ``amount`` is not here: it leaves as a ``refund_deposit`` payment.
    """
    sums = resolutions.aggregate(
        transferred=Sum("amount", filter=Q(kind=DepositResolutionKind.TRANSFERRED)),
        withheld=Sum("withheld_amount"),
    )
    return (Decimal(sums["transferred"] or ZERO) + Decimal(sums["withheld"] or ZERO)).quantize(CENT)


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
        lease = charge_lease(item)
        operator = lease.operator if lease is not None else None
        if operator is None and item.invoice is not None:
            operator = item.invoice.operator
        if operator is None and item.one_off_charge is not None:
            operator = item.one_off_charge.operator
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


def charge_lease(payment: Payment) -> Lease | None:
    """The lease of the payment's rent, insurance or one-off charge, if any."""
    for charge in (payment.rent_charge, payment.insurance_charge, payment.one_off_charge):
        if charge is not None:
            return charge.lease
    return None


def csv_record(row: LedgerRow) -> list[object]:
    payment = row.payment
    charge = payment.rent_charge
    return [
        payment.pk,
        payment.settled_on,
        payment.initiated_on,
        row.operator.display_name if row.operator is not None else "",
        row.store.external_label if row.store is not None else "",
        payment.kind,
        f"{charge.period_month:%Y-%m}" if charge is not None else "",
        charge.seq if charge is not None else None,
        payment.policy_year,
        payment.method,
        payment.status,
        payment.is_refund,
        payment.parent_payment_id,
        payment.amount,
        payment.reference,
        payment.note,
        row.has_note,
        row.transaction,
        payment.return_code,
        payment.recorded_by.get_username() if payment.recorded_by is not None else "",
        installment_of(payment),
    ]


def installment_of(payment: Payment) -> str:
    """What the plan of the payment's installment splits, or ``""`` (PLAN-012e D12)."""
    charge = payment.one_off_charge
    if charge is None or charge.plan is None:
        return ""
    return installments.parent_label(charge.plan.parent)


def _records(selected: PaymentFilter) -> Iterator[list[object]]:
    chunk: list[Payment] = []
    for payment in ledger_payments(selected).iterator(chunk_size=EXPORT_CHUNK):
        chunk.append(payment)
        if len(chunk) == EXPORT_CHUNK:
            yield from (csv_record(row) for row in rows_for(chunk))
            chunk = []
    yield from (csv_record(row) for row in rows_for(chunk))


def csv_lines(selected: PaymentFilter) -> Iterator[str]:
    """The export of :func:`ledger_payments`, one CSV line at a time."""
    return CsvWriter(CSV_HEADER).lines(_records(selected))

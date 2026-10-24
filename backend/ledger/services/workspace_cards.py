"""What the operator workspace's own cards read (docs/qa/PLAN-012f-a-design-follow-up.md D7;
DESIGN-SPEC §4 "Operator workspace", operator-workspace-laptop.png).

Read only; nothing here writes or calls the provider. Every value is a fact the database
already holds; nothing new is stored.

:func:`initials` is the header's round avatar: the first letters of the display name's first
and last words. :func:`store_cards` is the strip of store cards above the open tab: per store
of the operator, its title, the rent of the operator's current lease of it and how it is
collected ("bank debit on the 1st" while autopay is on, else what autopay is), its open balance
from the receivables rows the page already read and whether any of them is overdue; three
queries whatever the number of stores. :func:`returns_this_year` is the PAYMENT card's Returns
row (one query), :func:`bank_method` its Bank row (the linked method's stored record, no
provider call), :func:`open_promise` its Promise row, and :func:`statement_link_opened` the
statement link card's "Last opened" (one query on the audit rows the statement page writes).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from django.db.models import Prefetch, Q
from django.utils import timezone

from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    ContactEntry,
    Lease,
    LeaseRentHistory,
    Operator,
    Payment,
    PaymentStatus,
    ScheduleKind,
    ScheduleState,
    Store,
)
from ledger.services import contacts, leases, receivables, statement_links
from ledger.services import stores as store_service
from ledger.services.workspace import ordinal
from payments.provider import PaymentMethodRecord

ZERO = Decimal("0.00")
#: The strip card's collection line while autopay is not on (the store card's words).
AUTOPAY_NOT_SET_UP = "autopay not set up"
AUTOPAY_PAUSED = "autopay paused"
AUTOPAY_FAILED = "autopay failed"
NO_LEASE = "no current lease of this operator"


def initials(name: str) -> str:
    """ "FD" for "Fatih Düzenli", "F" for "fatih": the first letters of the first and last
    words, upper case; "?" for a name without a letter or digit."""
    words = [word for word in name.split() if word[:1].isalnum()]
    if not words:
        return "?"
    letters = words[0][0] + (words[-1][0] if len(words) > 1 else "")
    return letters.upper()


@dataclass(frozen=True, slots=True)
class StoreCard:
    """One card of the strip."""

    store: Store
    #: "#1991 Istanbul" (ledger.services.stores.title).
    title: str
    #: The rent in effect of the operator's current lease of the store, or ``None``.
    rent: Decimal | None
    #: "bank debit on the 1st", "autopay not set up", "autopay paused", "autopay failed" or
    #: :data:`NO_LEASE`.
    collect_line: str
    #: The store's open balance of this operator's charges.
    balance: Decimal
    #: Any of those charges is past its due date.
    overdue: bool


def _current_leases(
    operator: Operator, stores: Sequence[Store], today: date
) -> dict[uuid.UUID, Lease]:
    """Per store, its current lease (lease_end.current_lease's rule: not ended, active or
    paused, started by ``today``, the later start first) when it is the operator's; the
    rent rows in effect and the live rent schedules prefetched (three queries)."""
    found = (
        Lease.objects.filter(
            store__in=[store.pk for store in stores],
            ended_on__isnull=True,
            collection_status__in=[CollectionStatus.ACTIVE, CollectionStatus.PAUSED],
        )
        .filter(Q(started_on__isnull=True) | Q(started_on__lte=today))
        .order_by("store_id", "-started_on", "-created_at")
        .prefetch_related(
            Prefetch(
                "rent_history",
                queryset=LeaseRentHistory.objects.filter(effective_from__lte=today)
                .exclude(effective_to__lt=today)
                .order_by("-effective_from"),
            ),
            Prefetch(
                "schedules",
                queryset=CollectionSchedule.objects.filter(kind=ScheduleKind.RENT).exclude(
                    state=ScheduleState.ENDED
                ),
            ),
        )
    )
    current: dict[uuid.UUID, Lease] = {}
    for lease in found:
        current.setdefault(lease.store_id, lease)
    return {store: lease for store, lease in current.items() if lease.operator_id == operator.pk}


def _collect_line(lease: Lease) -> str:
    states = {row.state for row in lease.schedules.all()}
    if ScheduleState.ACTIVE in states:
        days = [day for day in (lease.debit_day, lease.second_debit_day) if day is not None]
        if days:
            return "bank debit on the " + " and ".join(ordinal(day) for day in days)
        return "bank debit"
    if ScheduleState.PAUSED in states:
        return AUTOPAY_PAUSED
    if ScheduleState.FAILED in states:
        return AUTOPAY_FAILED
    return AUTOPAY_NOT_SET_UP


def store_cards(
    operator: Operator,
    stores: Sequence[Store],
    rows: Iterable[receivables.OpenBalance],
    today: date,
) -> list[StoreCard]:
    """The strip's cards, in the order of ``stores`` (the selector's)."""
    if not stores:
        return []
    owed: dict[uuid.UUID, tuple[Decimal, bool]] = {}
    for row in rows:
        if row.store_id is None or row.balance <= 0:
            continue
        amount, overdue = owed.get(row.store_id, (ZERO, False))
        owed[row.store_id] = (amount + row.balance, overdue or row.overdue(today))
    current = _current_leases(operator, stores, today)
    cards = []
    for store in stores:
        lease = current.get(store.pk)
        rents = list(lease.rent_history.all()) if lease is not None else []
        balance, overdue = owed.get(store.pk, (ZERO, False))
        cards.append(
            StoreCard(
                store=store,
                title=store_service.title(store),
                rent=rents[0].amount if rents else None,
                collect_line=_collect_line(lease) if lease is not None else NO_LEASE,
                balance=balance,
                overdue=overdue,
            )
        )
    return cards


def _year_start(year: int) -> datetime:
    return timezone.make_aware(datetime.combine(date(year, 1, 1), time.min))


def returns_this_year(operator: Operator, today: date) -> int:
    """The operator's debits returned this calendar year (local time; one query), matched
    as the payments ledger matches a payment to its operator."""
    return (
        Payment.objects.filter(
            Q(rent_charge__lease__operator_id=operator.pk)
            | Q(insurance_charge__lease__operator_id=operator.pk)
            | Q(one_off_charge__operator_id=operator.pk)
            | Q(one_off_charge__lease__operator_id=operator.pk)
            | Q(invoice__operator_id=operator.pk),
            status=PaymentStatus.RETURNED,
            is_refund=False,
            returned_at__gte=_year_start(today.year),
            returned_at__lt=_year_start(today.year + 1),
        )
        .distinct()
        .count()
    )


def bank_method(operator: Operator, linked: str) -> PaymentMethodRecord | None:
    """The linked payment method's stored record (``linked``: its id, ``""`` when none is
    linked), or ``None``; no provider call."""
    return leases.linked_method_record(operator) if linked else None


def open_promise(operator: Operator) -> ContactEntry | None:
    """The operator's newest open promise, any store, not deleted."""
    return contacts.open_promises(operator).first()


def statement_link_opened(operator: Operator) -> datetime | None:
    """When the operator's statement page was last opened by its link (the audit row the
    page writes on every open), or ``None``."""
    found: datetime | None = (
        AuditLog.objects.filter(
            action=statement_links.OPENED_ACTION,
            entity_type=statement_links.ENTITY_OPERATOR,
            entity_id=str(operator.pk),
        )
        .order_by("-at", "-id")
        .values_list("at", flat=True)
        .first()
    )
    return found

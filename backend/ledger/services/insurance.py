"""Annual insurance charges (docs/inputs/OWNER_ANSWERS_ROUND2.md, ADR-0005; PLAN-007 D9).

The owner's algorithm, keyed to the lease (ADR-0005 point 4):

- The **anniversary** of year Y is the store's open date in year Y; 29 February falls
  on 28 February in a year that is not a leap year.
- Year Y is **in scope** when its anniversary is on or before today and, if the store
  has closed, before ``closed_at``.
- The lease that **holds the store on the anniversary** owes that year: its start date
  (when known) is on or before the anniversary and its end date (when known) is on or
  after it. Prospective leases owe nothing yet.
- No charge while the store ``needs_open_date``. A year without a premium gets a charge
  with ``amount = NULL`` and ``needs_premium``; it is never debited.

Generation is idempotent on ``(lease, policy_year)`` (a unique constraint plus
``get_or_create``). :func:`insurance_due_soon` only reads: it lists the anniversaries
of the coming days for the run screen, with the charge when one exists. Insurance
autopay schedules are a later plan (7c).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from ledger.models import (
    AuditLog,
    CollectionStatus,
    InsuranceCharge,
    InsurancePremium,
    Lease,
)

ENTITY_INSURANCE = "insurance_charge"
#: The run screen lists insurance due within this many days (owner's algorithm).
DUE_SOON_DAYS = 7


@dataclass(frozen=True, slots=True)
class InsuranceReport:
    created: int
    existing: int
    needs_premium: int
    #: Leases skipped because their store has no open date.
    needs_open_date: int


@dataclass(frozen=True, slots=True)
class InsuranceDue:
    lease: Lease
    policy_year: int
    due_on: date
    #: The year's premium, or ``None`` when it is not entered yet.
    amount: Decimal | None
    charge: InsuranceCharge | None


def anniversary(open_date: date, year: int) -> date:
    """The open date in ``year``; 29 February becomes 28 February in a common year."""
    if open_date.month == 2 and open_date.day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return open_date.replace(year=year)


def holds_on(lease: Lease, day: date) -> bool:
    """Whether the lease held the store on ``day`` (ADR-0005: that lease owes the year)."""
    if lease.started_on is not None and lease.started_on > day:
        return False
    return lease.ended_on is None or day <= lease.ended_on


def in_scope(lease: Lease, year: int, today: date) -> date | None:
    """The anniversary of ``year`` when the lease owes it as of ``today``, else ``None``."""
    store = lease.store
    if store.open_date is None:
        return None
    due = anniversary(store.open_date, year)
    if due > today or (store.closed_at is not None and due >= store.closed_at):
        return None
    return due if holds_on(lease, due) else None


def _leases() -> list[Lease]:
    return list(
        Lease.objects.select_related("store")
        .exclude(collection_status=CollectionStatus.PROSPECTIVE)
        .order_by("store__external_label", "id")
    )


def _premiums() -> dict[int, Decimal]:
    return dict(InsurancePremium.objects.values_list("policy_year", "amount"))


def generate_insurance_charges(today: date) -> InsuranceReport:
    """Create every in-scope charge that does not exist yet; return the counts."""
    premiums = _premiums()
    created = existing = needs_premium = needs_open_date = 0
    for lease in _leases():
        store = lease.store
        if store.needs_open_date or store.open_date is None:
            needs_open_date += 1
            continue
        for year in range(store.open_date.year, today.year + 1):
            due = in_scope(lease, year, today)
            if due is None:
                continue
            with transaction.atomic():
                charge, made = InsuranceCharge.objects.get_or_create(
                    lease=lease,
                    policy_year=year,
                    defaults={"due_on": due, "amount": premiums.get(year)},
                )
                if made:
                    AuditLog.objects.create(
                        actor=None,
                        action="insurance_charge.created",
                        entity_type=ENTITY_INSURANCE,
                        entity_id=str(charge.pk),
                        before=None,
                        after={
                            "lease": str(lease.pk),
                            "policy_year": year,
                            "due_on": due.isoformat(),
                            "amount": str(charge.amount) if charge.amount is not None else None,
                            "needs_premium": charge.needs_premium,
                        },
                    )
            if made:
                created += 1
                needs_premium += charge.needs_premium
            else:
                existing += 1
    return InsuranceReport(created, existing, needs_premium, needs_open_date)


def insurance_due_soon(today: date, days: int = DUE_SOON_DAYS) -> list[InsuranceDue]:
    """Anniversaries from ``today`` to ``today + days`` inclusive, by date. Read-only."""
    end = today + timedelta(days=days)
    premiums = _premiums()
    found: list[InsuranceDue] = []
    for lease in _leases():
        store = lease.store
        if store.open_date is None:
            continue
        for year in sorted({today.year, end.year}):
            if year < store.open_date.year:
                continue
            due = anniversary(store.open_date, year)
            if not today <= due <= end:
                continue
            if store.closed_at is not None and due >= store.closed_at:
                continue
            if not holds_on(lease, due):
                continue
            charge = lease.insurance_charges.filter(policy_year=year).first()
            found.append(InsuranceDue(lease, year, due, premiums.get(year), charge))
    return sorted(found, key=lambda item: (item.due_on, item.lease.store.external_label))

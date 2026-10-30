"""The reminder and overdue rules A to E (docs/qa/PLAN-014-per-lease-billing-messaging-portal-
deposits.md E1 to E4), queued once a day by ``collections_tick``.

**Rent before its charge: A and B by lease and month** (E4, the owner's decision of
2026-10-30). A month's rent charge is made on its debit day and no page shows rent early, so
the reminder three days ahead is keyed to the lease and the month, not to a charge
(:func:`rent_months_due`): every active lease, not ended and not ending before the debit,
whose first debit of the month (its debit day, as the rent run places it,
:func:`ledger.services.rent_run.debit_date`) is three days after ``today`` gets **A** when its
rent collection schedule is active (the schedule's amount, debited from its bank account),
else **B** (the rent in force on that day, due then). One message per lease, rule and month
(a :class:`~ledger.services.messaging.RentMonth`; the database keeps the one row): a lease
debited twice a month gets one, for the month's first debit (plan §8, a checkpoint
question). A month whose rent charge exists already and is paid, pending or waived (paid in
advance through the invoice builder, or collected) gets nothing, counted skipped; a lease
with no rent in force on the day gets no charge from the rent run and no message.

**Which charges.** The open balances (:func:`ledger.services.receivables.open_balances`):
rent, insurance and one-off charges with something owed, an installment plan's rows each on
its own due date (its parent is not open while the plan is live), deposits never. A charge
whose status closes it to messages (rent ``pending``, ``paid`` or ``waived``; one-off
``paid``, ``waived`` or ``voided``, :data:`ledger.services.messaging.CLOSED_RENT` and
``CLOSED_ONE_OFF``) gets nothing.

**Which rule** (:func:`rule_for`), counted in New York days from the charge's due date:

- **A**, and **B** for rent — never from a charge: they come from the lease and the month
  (above), before the charge exists.
- A rent charge on autopay (its lease has an active rent collection schedule, and no payment
  of the charge was returned) gets nothing more: its debit collects it. Once a debit of it is
  returned, or the schedule is no longer active, the charge takes the rules below like any
  other.
- **B** — any other open charge (insurance, one-off), three days before its due date.
- **C** — five days after it.
- **D** — ten days after it; then again (occurrence 2, 3, …) at 40, 50, 60 … days while
  something is owed. The 20-day gap between 10 and 40 is the owner's rule.
- **E** — thirty days after it, once, internal: to the managers' list of the company
  settings, never to the operator (an empty list holds it, ``no_manager_list``).

Only the rule of ``today`` fires: a day that has passed is never sent later, so the first
run, or a run after a pause, does not flood an operator with the rules it missed.

**Pause** (E2). An open promise to pay whose date is today or later
(:func:`ledger.services.contacts.promises_pending`) on the lease that bills the charge, or
on no store of the same operator, pauses B to E (:data:`PAUSED_BY_PROMISE`) for every charge
of that lease and for its month's B; A still goes, since the debit will happen anyway. A
promise past its date is broken and pauses nothing: the rules resume from the next rule day,
without catching up the days paused.

**Queue** (:func:`queue_due`). The lease months first, then the open balances. The lease
that bills a charge is
:func:`ledger.services.leases.billing_lease` (a one-off charge without a lease bills its
operator's first active lease; a charge with none is skipped). Each message is queued
through :func:`ledger.services.messaging.queue` with the rule's seeded template, under the
lease's row lock (``of=("self",)``: never a lock over a nullable join, which PostgreSQL
refuses). The database keeps one message per (lease, charge, rule, occurrence), and per
(lease, rule, month) for a lease month, so a second run the same day queues nothing more and
counts it skipped. Whether the message then goes is the engine's: an operator handled
personally or a lease without a billing e-mail is held, an archived operator or a charge
closed before the send is suppressed, as is a lease month whose lease ended, whose A lost
its schedule, or whose rent charge closed since (``LeaseEnded``, ``ScheduleStopped``,
``MonthSettled``).

**No fee** (E3). Nothing here creates a charge or a fee, and no rule's text speaks of one.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from ledger import merge
from ledger.models import (
    CollectionSchedule,
    CollectionStatus,
    InsuranceCharge,
    Lease,
    MessageRule,
    MessageTemplate,
    OneOffCharge,
    Payment,
    PaymentChannel,
    PaymentStatus,
    RentCharge,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import contacts, leases, messaging, receivables, rent_run
from ledger.services.messaging import RentMonth

logger = logging.getLogger(__name__)

#: The rules of this module, in the order the tick prints them.
RULES: tuple[str, ...] = (
    MessageRule.A,
    MessageRule.B,
    MessageRule.C,
    MessageRule.D,
    MessageRule.E,
)
#: The rules an open promise pauses (E2); A still goes: the debit happens anyway.
PAUSED_BY_PROMISE: frozenset[str] = frozenset(
    {MessageRule.B, MessageRule.C, MessageRule.D, MessageRule.E}
)
#: The seeded template of each rule (plan §9).
TEMPLATES: Mapping[str, str] = {
    MessageRule.A: "reminder_autopay",
    MessageRule.B: "reminder_due",
    MessageRule.C: "overdue_day5",
    MessageRule.D: "overdue_day10",
    MessageRule.E: "internal_day30",
}

#: A and B: days before the debit or due date.
DAYS_BEFORE = 3
#: C, D and E: days after the due date.
DAY_C = 5
DAY_D = 10
DAY_E = 30
#: D again from this day, every :data:`D_EVERY` days.
D_AGAIN_FROM = 40
D_EVERY = 10


@dataclass(frozen=True, slots=True)
class Rule:
    """The rule due for one charge on one day: its letter, its template's code and its
    occurrence (D's second notice is occurrence 2)."""

    code: str
    template: str
    occurrence: int = 1


@dataclass(frozen=True, slots=True)
class Summary:
    """What :func:`queue_due` did: the messages queued per rule (every rule present), the
    charges whose rule an open promise paused, and the ones not queued (already queued
    that day, no lease or operator to bill, or the rule's template inactive or refused)."""

    queued_by_rule: Mapping[str, int]
    paused: int
    skipped: int

    @property
    def queued(self) -> int:
        return sum(self.queued_by_rule.values())


def _rule(code: str, occurrence: int = 1) -> Rule:
    return Rule(code, TEMPLATES[code], occurrence)


def _closed(row: receivables.OpenBalance) -> bool:
    """Whether the charge's status closes it to messages, as the engine reads it at send."""
    if row.balance <= 0:
        return True
    if row.kind == receivables.KIND_RENT:
        return row.status in messaging.CLOSED_RENT
    if row.kind == receivables.KIND_ONE_OFF:
        return row.status in messaging.CLOSED_ONE_OFF
    return False


def on_autopay(rows: Iterable[receivables.OpenBalance]) -> set[uuid.UUID]:
    """The rent charges among ``rows`` that are collected by autopay: their lease has an
    active rent collection schedule and no payment of the charge was returned. One query."""
    ids = [row.charge_id for row in rows if row.kind == receivables.KIND_RENT]
    if not ids:
        return set()
    schedule = CollectionSchedule.objects.filter(
        lease=OuterRef("lease"), kind=ScheduleKind.RENT, state=ScheduleState.ACTIVE
    )
    returned = Payment.objects.filter(rent_charge=OuterRef("pk"), status=PaymentStatus.RETURNED)
    return set(
        RentCharge.objects.filter(pk__in=ids)
        .filter(Exists(schedule))
        .exclude(Exists(returned))
        .values_list("pk", flat=True)
    )


def _d_occurrence(late: int) -> int | None:
    if late == DAY_D:
        return 1
    if late >= D_AGAIN_FROM and (late - D_AGAIN_FROM) % D_EVERY == 0:
        return 2 + (late - D_AGAIN_FROM) // D_EVERY
    return None


def rule_for(
    row: receivables.OpenBalance, today: date, *, autopay: bool | None = None
) -> Rule | None:
    """The rule due for the open charge ``row`` on ``today`` (the module docstring), or
    ``None``. ``autopay`` says whether it is a rent charge collected by autopay
    (:func:`on_autopay`); read here (one query) when not given. A and B for rent are never a
    charge's (:func:`rent_months_due`). A promise's pause is not applied here:
    :func:`queue_due` counts it."""
    if _closed(row):
        return None
    lead = (row.due_on - today).days
    if row.kind == receivables.KIND_RENT:
        if lead == DAYS_BEFORE:
            return None
        if autopay is None:
            autopay = row.charge_id in on_autopay([row])
        if autopay:
            return None
    late = -lead
    if lead == DAYS_BEFORE:
        return _rule(MessageRule.B)
    if late == DAY_C:
        return _rule(MessageRule.C)
    if late == DAY_E:
        return _rule(MessageRule.E)
    occurrence = _d_occurrence(late)
    return _rule(MessageRule.D, occurrence) if occurrence is not None else None


def _active_rent_schedules(lease_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    """Lease -> the amount of its first active rent schedule (one query)."""
    found: dict[uuid.UUID, Decimal] = {}
    rows = (
        CollectionSchedule.objects.filter(
            lease_id__in=list(lease_ids), kind=ScheduleKind.RENT, state=ScheduleState.ACTIVE
        )
        .order_by("lease_id", "seq")
        .values_list("lease_id", "amount")
    )
    for lease_id, amount in rows:
        found.setdefault(lease_id, amount)
    return found


def rent_months_due(today: date) -> list[tuple[RentMonth, Rule]]:
    """The lease months whose A or B falls on ``today`` (the module docstring): the leases
    whose month's first debit is :data:`DAYS_BEFORE` days ahead, with A (autopay, the
    schedule's amount) or B (the rent in force on the day). A month already settled is
    listed: :func:`queue_due` counts it skipped."""
    debit = today + timedelta(days=DAYS_BEFORE)
    month = debit.replace(day=1)
    days = [
        day for day in range(1, 32) if rent_run.falls_on(day, debit.year, debit.month) == debit.day
    ]
    candidates = list(
        Lease.objects.select_related("store", "operator", "billing")
        .filter(
            collection_status=CollectionStatus.ACTIVE,
            operator__isnull=False,
            debit_day__isnull=False,
        )
        .exclude(payment_channel=PaymentChannel.UNKNOWN)
        .filter(Q(started_on__isnull=True) | Q(started_on__lte=debit))
        .filter(Q(ended_on__isnull=True) | Q(ended_on__gte=debit))
        # A lease starting in the month after its debit day is first due on its start.
        .filter(Q(debit_day__in=days) | Q(started_on=debit))
        .order_by("store__external_label", "id")
    )
    due = [
        lease
        for lease in candidates
        if lease.debit_day is not None
        and rent_run.debit_date(lease, month, lease.debit_day) == debit
    ]
    schedules = _active_rent_schedules(lease.pk for lease in due)
    found: list[tuple[RentMonth, Rule]] = []
    for lease in due:
        if lease.pk in schedules:
            found.append(
                (RentMonth(lease, month, debit, schedules[lease.pk]), _rule(MessageRule.A))
            )
            continue
        rent = leases.rent_on(lease, debit)
        if rent is not None:
            found.append((RentMonth(lease, month, debit, rent.amount), _rule(MessageRule.B)))
    return found


def _charges(
    rows: Iterable[receivables.OpenBalance],
) -> dict[tuple[str, uuid.UUID], messaging.Charge]:
    """(kind, id) -> the charge of each row, with its lease, store and operator (three
    queries at most)."""
    ids: dict[str, list[uuid.UUID]] = {}
    for row in rows:
        ids.setdefault(row.kind, []).append(row.charge_id)
    found: dict[tuple[str, uuid.UUID], messaging.Charge] = {}
    leased = ("lease__store", "lease__operator", "lease__billing")
    if receivables.KIND_RENT in ids:
        for rent in RentCharge.objects.select_related(*leased).filter(
            pk__in=ids[receivables.KIND_RENT]
        ):
            found[(receivables.KIND_RENT, rent.pk)] = rent
    if receivables.KIND_INSURANCE in ids:
        for policy in InsuranceCharge.objects.select_related(*leased).filter(
            pk__in=ids[receivables.KIND_INSURANCE]
        ):
            found[(receivables.KIND_INSURANCE, policy.pk)] = policy
    if receivables.KIND_ONE_OFF in ids:
        for one_off in OneOffCharge.objects.select_related("operator", "fee_type", *leased).filter(
            pk__in=ids[receivables.KIND_ONE_OFF]
        ):
            found[(receivables.KIND_ONE_OFF, one_off.pk)] = one_off
    return found


def _paused(
    lease: Lease, operator_id: uuid.UUID, pending: set[tuple[uuid.UUID, uuid.UUID | None]]
) -> bool:
    return (operator_id, lease.pk) in pending or (operator_id, None) in pending


def queue_due(today: date) -> Summary:
    """Queue the message of the rule due today for every lease month
    (:func:`rent_months_due`), then for every open charge (the module docstring): one row
    and its ``message_send`` job each."""
    months = rent_months_due(today)
    rows = receivables.open_balances(today)
    autopay = on_autopay(rows)
    due = [
        (row, rule)
        for row in rows
        if (rule := rule_for(row, today, autopay=row.charge_id in autopay)) is not None
    ]
    queued = dict.fromkeys(RULES, 0)
    if not due and not months:
        return Summary(queued, 0, 0)
    templates = {
        row.code: row
        for row in MessageTemplate.objects.filter(code__in=TEMPLATES.values(), active=True)
    }
    pending = contacts.promises_pending(today)
    paused = skipped = 0
    for rent_month, rule in months:
        owner = rent_month.lease.operator
        template = templates.get(rule.template)
        if owner is None or template is None:
            skipped += 1
            continue
        if rule.code in PAUSED_BY_PROMISE and _paused(rent_month.lease, owner.pk, pending):
            paused += 1
            continue
        if _queue_month(rent_month, rule, template, today):
            queued[rule.code] += 1
        else:
            skipped += 1
    charges = _charges(row for row, _ in due)
    for row, rule in due:
        charge = charges.get((row.kind, row.charge_id))
        lease = leases.billing_lease(charge) if charge is not None else None
        operator = messaging.operator_of(lease, charge) if charge is not None else None
        template = templates.get(rule.template)
        if charge is None or lease is None or operator is None or template is None:
            skipped += 1
            continue
        if rule.code in PAUSED_BY_PROMISE and _paused(lease, operator.pk, pending):
            paused += 1
            continue
        if _queue(lease, charge, rule, template, row, today):
            queued[rule.code] += 1
        else:
            skipped += 1
    return Summary(queued, paused, skipped)


def _queue(
    lease: Lease,
    charge: messaging.Charge,
    rule: Rule,
    template: MessageTemplate,
    row: receivables.OpenBalance,
    today: date,
) -> bool:
    """Queue one rule's message under the lease's row lock; ``False`` when the row exists
    already, or when the template cannot be rendered or sent (logged, by class name)."""
    with transaction.atomic():
        Lease.objects.select_for_update(of=("self",)).filter(pk=lease.pk).first()
        context = messaging.context_for(
            lease,
            charge,
            rule.code,
            today=today,
            # What the open balances owe (a re-debit may carry part of the charge).
            amount=row.balance,
        )
        try:
            message = messaging.queue(
                rule.code, lease, charge, template, context, occurrence=rule.occurrence
            )
        except (messaging.ChannelUnavailable, merge.TemplateRefused) as error:
            logger.warning(
                "Reminder %s not queued for charge %s: %s",
                rule.code,
                row.charge_id,
                type(error).__name__,
            )
            return False
    return message is not None


def _queue_month(rent_month: RentMonth, rule: Rule, template: MessageTemplate, today: date) -> bool:
    """Queue a lease month's A or B under the lease's row lock; ``False`` when the month's
    rent charge is settled already, the row exists already, or the template cannot be
    rendered or sent (logged, by class name)."""
    lease = rent_month.lease
    with transaction.atomic():
        Lease.objects.select_for_update(of=("self",)).filter(pk=lease.pk).first()
        if messaging.month_settled(lease.pk, rent_month.month):
            return False
        context = messaging.context_for(lease, rent_month, rule.code, today=today)
        try:
            message = messaging.queue(rule.code, lease, rent_month, template, context)
        except (messaging.ChannelUnavailable, merge.TemplateRefused) as error:
            logger.warning(
                "Reminder %s not queued for lease %s: %s",
                rule.code,
                lease.pk,
                type(error).__name__,
            )
            return False
    return message is not None

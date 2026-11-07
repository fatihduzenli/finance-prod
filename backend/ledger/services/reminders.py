"""The reminder and overdue rules A to E (docs/qa/PLAN-014-per-lease-billing-messaging-portal-
deposits.md E1 to E4), queued once a day by ``collections_tick``.

**Rent before its charge: A and B by lease and month** (E4, the owner's decision of
2026-10-30). A month's rent charge is made on its debit day and no page shows rent early, so
the reminder three days ahead is keyed to the lease and the month, not to a charge
(:func:`rent_months_due`): every active lease, not ended and not ending before the debit,
whose first debit of the month (its debit day, as the rent run places it,
:func:`ledger.services.rent_run.debit_date`) is three days after ``today`` gets **A** when its
rent collection schedule is active (the schedule's amount, debited from its bank account),
else **B** (the amount the rent run charges for that debit, prorated and split as
:func:`ledger.services.rent_run.create_month` does, due then). One message per lease, rule and month
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

**Collection paused** (PLAN-015b §3 B). A lease whose collection is paused gets no A or B for
its months (only active leases are listed) and no B to E for the charges it bills (skipped,
"collection is paused"); a message queued before the pause is suppressed at the send
(``LeasePaused``). Once collection resumes the rules go again from the next rule day.

**Plan, then queue** (:func:`plan`, :func:`queue_due`). :func:`plan` reads what the day's rules
would queue, writing nothing: per lease month and open charge its rule, template, lease (and
store), operator, amount and outcome (:class:`Planned`) — it would be queued, paused by a
promise, or skipped and why (no lease or operator to bill, no active template, the month
settled, already queued); a message that would be queued to a lease without a billing e-mail
says it will be held, as does one that is not internal while the company name or support
e-mail is blank (``held: settings incomplete``,
:func:`ledger.services.messaging.settings_incomplete`). :func:`queue_due` queues exactly the
planned items that would be queued, so the Outbox's preview of the next 30 days
(:mod:`ledger.services.outbox_preview`) and the daily job can never disagree, but for a
template that cannot be rendered, which only the queue finds (counted skipped).

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

**One e-mail per lease and day, and the 3-day fold** (owner, 2026-11-02). The rows are queued
as above, one per charge, rule and occurrence, but the operator's rules A to D of one lease and
day are then sent as one message (:func:`messaging.combine`): the most urgent
(:data:`COMBINED_RULES`, D > C > B > A) lists every charge, the others are combined into it. E
goes to the managers and stays on its own; F follows a return and is not this job's. An overdue
row (C or D, :data:`OVERDUE_RULES`) for a lease whose last overdue message went less than
:data:`FOLD_DAYS` days before (:func:`last_overdue`: queued, sent or held, counted on the day it
goes, not a row combined into another) waits instead (:func:`fold`,
:func:`messaging.wait_for_fold`) and goes on the first day after them, combined with what is due
that day, or on its own when nothing is; a charge closed meanwhile drops out
(:func:`messaging.release`). B and A never wait. The Outbox's preview applies the same
:func:`fold` day by day.

**No fee** (E3). Nothing here creates a charge or a fee, and no rule's text speaks of one.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Coalesce

from finance.dates import NEW_YORK
from ledger import merge
from ledger.models import (
    ChargeEntityType,
    CollectionSchedule,
    CollectionStatus,
    CompanySettings,
    InsuranceCharge,
    Lease,
    LeaseBilling,
    MessageRule,
    MessageStatus,
    MessageTemplate,
    OneOffCharge,
    Operator,
    OutboundMessage,
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

#: The operator's rules sent as one message per lease and day, most urgent first (owner,
#: 2026-11-02; the engine's own order, which the send also reads).
COMBINED_RULES: tuple[str, ...] = messaging.COMBINED_RULES
#: The overdue rules: at most one message of them per lease every :data:`FOLD_DAYS` days.
OVERDUE_RULES: frozenset[str] = frozenset({MessageRule.C, MessageRule.D})
FOLD_DAYS = 3
#: The statuses of an overdue message that counts as given to the lease (it went, will go,
#: or waits in the Outbox for a setting).
GIVEN: tuple[str, ...] = (MessageStatus.QUEUED, MessageStatus.SENT, MessageStatus.HELD)


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
    schedule's amount) or B (what the rent run charges for the month's first debit,
    :func:`ledger.services.rent_run.first_debit_amount`: prorated in the lease's first month,
    half for a lease debited twice a month). A month already settled is
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
        amount = rent_run.first_debit_amount(lease, month)
        if amount is not None:
            found.append((RentMonth(lease, month, debit, amount), _rule(MessageRule.B)))
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


#: What :func:`plan` says of one message (the preview's outcome).
QUEUE = "queue"
PAUSED = "paused"
NO_LEASE = "no_lease"
NO_TEMPLATE = "no_template"
MONTH_SETTLED = "month_settled"
ALREADY_QUEUED = "already_queued"
#: PLAN-015b §3 B: the lease that bills the charge has its collection paused.
COLLECTION_PAUSED = "collection_paused"
OUTCOME_TEXT: Mapping[str, str] = {
    QUEUE: "Would queue",
    PAUSED: "Paused by a promise",
    NO_LEASE: "Skipped: no lease or operator to bill",
    NO_TEMPLATE: "Skipped: no active template",
    MONTH_SETTLED: "Skipped: the month is settled",
    ALREADY_QUEUED: "Skipped: already queued",
    COLLECTION_PAUSED: "Skipped: collection is paused",
}
#: Added to "Would queue" when the lease has no billing e-mail: the send holds it.
HELD_NO_EMAIL = "held: no billing e-mail"
#: Added to "Would queue" for a message that is not internal while the company name or support
#: e-mail is blank (owner, 2026-11-02): it is held ``settings_incomplete``.
HELD_SETTINGS = "held: settings incomplete"


@dataclass(frozen=True, slots=True)
class Planned:
    """One message :func:`plan` finds for ``day``: about a lease month (``row`` ``None``) or
    an open charge (``row`` its open balance; ``subject`` ``None`` when the charge was not
    found), with the lease that bills it, its operator, the rule's active template, the amount
    and the outcome (:data:`OUTCOME_TEXT`). ``no_email``: the lease has no billing e-mail (an
    operator-facing message would be held); ``settings_held``: it is not internal and the
    company name or support e-mail is blank (it would be held ``settings_incomplete``)."""

    day: date
    rule: Rule
    subject: messaging.Subject | None
    row: receivables.OpenBalance | None
    lease: Lease | None
    operator: Operator | None
    template: MessageTemplate | None
    amount: Decimal
    outcome: str
    no_email: bool = False
    settings_held: bool = False

    @property
    def queues(self) -> bool:
        return self.outcome == QUEUE

    @property
    def key(self) -> tuple[object, ...]:
        """What the job's idempotency keeps once: (lease, rule, month) for a lease month;
        (charge kind, charge, rule, occurrence) for a charge."""
        if isinstance(self.subject, RentMonth):
            return (self.subject.lease.pk, self.rule.code, self.subject.month)
        row = self.row
        return (row.kind, row.charge_id, self.rule.code, self.rule.occurrence) if row else ()

    @property
    def outcome_text(self) -> str:
        text = OUTCOME_TEXT[self.outcome]
        if self.queues and self.settings_held:
            return f"{text} ({HELD_SETTINGS})"
        return f"{text} ({HELD_NO_EMAIL})" if self.queues and self.no_email else text


def _settled_months(months: list[tuple[RentMonth, Rule]]) -> set[tuple[uuid.UUID, date]]:
    """(lease, month) of the lease months whose rent charge is settled already (one query)."""
    if not months:
        return set()
    return set(
        RentCharge.objects.filter(
            lease_id__in=[month.lease.pk for month, _ in months],
            period_month__in={month.month for month, _ in months},
            seq=1,
            status__in=messaging.CLOSED_RENT,
        ).values_list("lease_id", "period_month")
    )


def _row_key(item: Planned) -> tuple[object, ...] | None:
    """The unique row ``item`` would be: (lease, rule, month) for a lease month; (lease,
    charge type, charge, rule, occurrence) for a charge; ``None`` without a lease."""
    if item.lease is None or item.subject is None:
        return None
    if isinstance(item.subject, RentMonth):
        return (item.lease.pk, item.rule.code, item.subject.month)
    return (
        item.lease.pk,
        messaging.charge_type_of(item.subject),
        item.subject.pk,
        item.rule.code,
        item.rule.occurrence,
    )


def _already_queued(items: list[Planned]) -> set[tuple[object, ...]]:
    """The :func:`_row_key` of each of ``items`` that a rule's row holds already (two queries
    at most)."""
    months: list[RentMonth] = [
        item.subject for item in items if isinstance(item.subject, RentMonth)
    ]
    charge_ids = [
        item.subject.pk
        for item in items
        if item.subject is not None and not isinstance(item.subject, RentMonth)
    ]
    found: set[tuple[object, ...]] = set()
    if months:
        found.update(
            OutboundMessage.objects.filter(
                manual=False,
                charge_entity_type=ChargeEntityType.LEASE_MONTH,
                lease_id__in=[month.lease.pk for month in months],
                period_month__in={month.month for month in months},
            ).values_list("lease_id", "rule", "period_month")
        )
    if charge_ids:
        found.update(
            OutboundMessage.objects.filter(
                manual=False, period_month__isnull=True, charge_entity_id__in=charge_ids
            ).values_list(
                "lease_id", "charge_entity_type", "charge_entity_id", "rule", "occurrence"
            )
        )
    return found


def _billing_emails(items: list[Planned]) -> dict[uuid.UUID, str]:
    """Lease -> its billing e-mail, for the leases of ``items`` (one query)."""
    ids = {item.lease.pk for item in items if item.lease is not None}
    if not ids:
        return {}
    return dict(LeaseBilling.objects.filter(lease_id__in=ids).values_list("lease_id", "email"))


def plan(today: date) -> list[Planned]:
    """What the rules due ``today`` would queue (the module docstring), writing nothing: the
    lease months first (:func:`rent_months_due`), then the open charges, each with its
    outcome. :func:`queue_due` queues exactly the items that :attr:`Planned.queues`."""
    months = rent_months_due(today)
    rows = receivables.open_balances(today)
    autopay = on_autopay(rows)
    due = [
        (row, rule)
        for row in rows
        if (rule := rule_for(row, today, autopay=row.charge_id in autopay)) is not None
    ]
    if not due and not months:
        return []
    templates = {
        row.code: row
        for row in MessageTemplate.objects.filter(code__in=TEMPLATES.values(), active=True)
    }
    pending = contacts.promises_pending(today)
    settled = _settled_months(months)
    items: list[Planned] = []
    for rent_month, rule in months:
        lease = rent_month.lease
        owner = lease.operator
        template = templates.get(rule.template)
        if owner is None:
            outcome = NO_LEASE
        elif template is None:
            outcome = NO_TEMPLATE
        elif rule.code in PAUSED_BY_PROMISE and _paused(lease, owner.pk, pending):
            outcome = PAUSED
        elif (lease.pk, rent_month.month) in settled:
            outcome = MONTH_SETTLED
        else:
            outcome = QUEUE
        items.append(
            Planned(
                today, rule, rent_month, None, lease, owner, template, rent_month.amount, outcome
            )
        )
    charges = _charges(row for row, _ in due)
    for row, rule in due:
        charge = charges.get((row.kind, row.charge_id))
        billed = leases.billing_lease(charge) if charge is not None else None
        operator = messaging.operator_of(billed, charge) if charge is not None else None
        template = templates.get(rule.template)
        if charge is None or billed is None or operator is None:
            outcome = NO_LEASE
        elif billed.collection_status == CollectionStatus.PAUSED:
            outcome = COLLECTION_PAUSED
        elif template is None:
            outcome = NO_TEMPLATE
        elif rule.code in PAUSED_BY_PROMISE and _paused(billed, operator.pk, pending):
            outcome = PAUSED
        else:
            outcome = QUEUE
        items.append(
            Planned(today, rule, charge, row, billed, operator, template, row.balance, outcome)
        )
    return _with_queued_and_emails(items)


def _with_queued_and_emails(items: list[Planned]) -> list[Planned]:
    """``items`` with the ones a row holds already marked so, and the e-mail-less and the ones
    held for the company settings marked."""
    candidates = [item for item in items if item.queues]
    queued = _already_queued(candidates)
    emails = _billing_emails(candidates)
    incomplete = bool(candidates) and bool(messaging.settings_incomplete(CompanySettings.read()))
    found: list[Planned] = []
    for item in items:
        if item.queues and _row_key(item) in queued:
            item = replace(item, outcome=ALREADY_QUEUED)
        elif item.queues and item.lease is not None and item.template is not None:
            internal = item.template.internal
            item = replace(
                item,
                no_email=not internal and not emails.get(item.lease.pk),
                settings_held=not internal and incomplete,
            )
        found.append(item)
    return found


# --- One e-mail per lease and day, and the 3-day fold (owner, 2026-11-02) ----------------------


def urgency(rule: str) -> int:
    """The rank of ``rule`` in :data:`COMBINED_RULES` (0 the most urgent)."""
    return COMBINED_RULES.index(rule)


def fold_until(last: date | None, today: date) -> date | None:
    """The day an overdue message due ``today`` may go, when the lease's last overdue one was
    queued on ``last`` less than :data:`FOLD_DAYS` days before; ``None`` when it may go
    today."""
    if last is None or not 0 <= (today - last).days < FOLD_DAYS:
        return None
    return last + timedelta(days=FOLD_DAYS)


def fold[T](
    items: Sequence[T], rule_of: Callable[[T], str], last: date | None, today: date
) -> tuple[list[T], list[T], date | None]:
    """Split one lease's ``items`` of ``today`` by the fold (:func:`fold_until` of ``last``,
    the day of its last overdue message): what goes today, most urgent first; what waits;
    and the day the fold ends (``None`` when it does not hold today: nothing waits, and the
    messages waiting from before go today)."""
    until = fold_until(last, today)
    going: list[T] = []
    held: list[T] = []
    for item in items:
        (held if until is not None and rule_of(item) in OVERDUE_RULES else going).append(item)
    going.sort(key=lambda item: urgency(rule_of(item)))
    return going, held, until


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=NEW_YORK)


def last_overdue(
    today: date, lease_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, date]:
    """Lease -> the New York day its last overdue message (C or D, :data:`GIVEN`, not
    combined into another) goes, its send window's day (``scheduled_for``; a waiting message
    sent later is counted on the day it went), in the :data:`FOLD_DAYS` days up to ``today``;
    only ``lease_ids`` when given (one query)."""
    found = (
        OutboundMessage.objects.filter(
            rule__in=OVERDUE_RULES, status__in=GIVEN, manual=False, lease__isnull=False
        )
        .annotate(goes=Coalesce("scheduled_for", "queued_at"))
        .filter(
            goes__gte=_start_of(today - timedelta(days=FOLD_DAYS - 1)),
            goes__lt=_start_of(today + timedelta(days=1)),
        )
    )
    if lease_ids is not None:
        found = found.filter(lease_id__in=list(lease_ids))
    days: dict[uuid.UUID, date] = {}
    for lease_id, goes in found.values_list("lease_id", "goes"):
        day = goes.astimezone(NEW_YORK).date()
        if lease_id is not None and day > days.get(lease_id, date.min):
            days[lease_id] = day
    return days


def waiting(today: date | None = None, lease_id: uuid.UUID | None = None) -> list[OutboundMessage]:
    """The messages waiting for the fold, oldest first: only those whose day is ``today`` or
    before when given, of the lease ``lease_id`` when given."""
    found = OutboundMessage.objects.filter(status=MessageStatus.WAITING).select_related(
        "lease__store", "operator", "template"
    )
    if today is not None:
        found = found.filter(scheduled_for__lt=_start_of(today + timedelta(days=1)))
    if lease_id is not None:
        found = found.filter(lease_id=lease_id)
    return list(found.order_by(Coalesce("scheduled_for", "queued_at"), "queued_at", "id"))


def queue_due(today: date) -> Summary:
    """Queue the message of every item :func:`plan` would queue today (the module
    docstring): one row each, then per lease one ``message_send`` job for the operator's
    rules (:func:`_send_lease`) and one each for E; the paused counted paused, the rest
    skipped."""
    queued = dict.fromkeys(RULES, 0)
    paused = skipped = 0
    by_lease: dict[uuid.UUID, list[Planned]] = {}
    for item in plan(today):
        if item.outcome == PAUSED:
            paused += 1
            continue
        if not item.queues or item.lease is None or item.template is None:
            skipped += 1
            continue
        if item.rule.code in COMBINED_RULES:
            by_lease.setdefault(item.lease.pk, []).append(item)
            continue
        if _queue_item(item, today, job=True) is not None:
            queued[item.rule.code] += 1
        else:
            skipped += 1
    released: dict[uuid.UUID, Lease] = {}
    for message in waiting(today):
        if message.lease is not None and message.lease.pk not in by_lease:
            released.setdefault(message.lease.pk, message.lease)
    for lease_id in [*by_lease, *released]:
        items = by_lease.get(lease_id, [])
        lease = items[0].lease if items else released[lease_id]
        if lease is None:  # never so: a planned item to queue has its lease
            continue
        done = _send_lease(lease, items, today)
        for item in items:
            if _row_key(item) in done:
                queued[item.rule.code] += 1
            else:
                skipped += 1
    return Summary(queued, paused, skipped)


def _send_lease(lease: Lease, items: list[Planned], today: date) -> set[tuple[object, ...]]:
    """Under the lease's row lock: queue ``items`` without their jobs, then send them, and the
    lease's waiting messages once their day has come, as one message (the module docstring);
    the :func:`_row_key` of each item queued."""
    with transaction.atomic():
        Lease.objects.select_for_update(of=("self",)).filter(pk=lease.pk).first()
        last = last_overdue(today, [lease.pk]).get(lease.pk)
        new: list[tuple[OutboundMessage, messaging.ChargeLine]] = []
        done: set[tuple[object, ...]] = set()
        for item in items:
            message = _queue_item(item, today, job=False)
            key = _row_key(item)
            if message is None or item.subject is None or key is None:
                continue
            done.add(key)
            if message.status == MessageStatus.QUEUED:
                new.append((message, messaging.line_for(item.subject, item.amount, today)))
        going, held, until = fold(new, lambda pair: pair[0].rule, last, today)
        if until is not None:
            messaging.wait_for_fold((message for message, _ in held), until)
        else:
            released = messaging.release(waiting(lease_id=lease.pk))
            charges = messaging.charges_of(released)
            for message in released:
                charge = charges.get(message.pk)
                if charge is not None:
                    line = messaging.line_for(charge, messaging.balance_of(charge), today)
                    going.append((message, line))
            going.sort(key=lambda pair: urgency(pair[0].rule))
        if going:
            messaging.combine(going)
    return done


def _queue_item(item: Planned, today: date, *, job: bool) -> OutboundMessage | None:
    """Queue one planned item's row (with its job when ``job``); ``None`` when it was not."""
    if item.lease is None or item.template is None:
        return None
    if isinstance(item.subject, RentMonth):
        return _queue_month(item.subject, item.rule, item.template, today, job=job)
    if item.subject is not None and item.row is not None:
        return _queue(item.lease, item.subject, item.rule, item.template, item.row, today, job=job)
    return None  # never planned so: a charge to queue has its row


def _queue(
    lease: Lease,
    charge: messaging.Charge,
    rule: Rule,
    template: MessageTemplate,
    row: receivables.OpenBalance,
    today: date,
    *,
    job: bool = True,
) -> OutboundMessage | None:
    """Queue one rule's message under the lease's row lock; ``None`` when the row exists
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
            return messaging.queue(
                rule.code, lease, charge, template, context, occurrence=rule.occurrence, job=job
            )
        except (messaging.ChannelUnavailable, merge.TemplateRefused) as error:
            logger.warning(
                "Reminder %s not queued for charge %s: %s",
                rule.code,
                row.charge_id,
                type(error).__name__,
            )
            return None


def _queue_month(
    rent_month: RentMonth,
    rule: Rule,
    template: MessageTemplate,
    today: date,
    *,
    job: bool = True,
) -> OutboundMessage | None:
    """Queue a lease month's A or B under the lease's row lock; ``None`` when the month's
    rent charge is settled already, the row exists already, or the template cannot be
    rendered or sent (logged, by class name)."""
    lease = rent_month.lease
    with transaction.atomic():
        Lease.objects.select_for_update(of=("self",)).filter(pk=lease.pk).first()
        if messaging.month_settled(lease.pk, rent_month.month):
            return None
        context = messaging.context_for(lease, rent_month, rule.code, today=today)
        try:
            return messaging.queue(rule.code, lease, rent_month, template, context, job=job)
        except (messaging.ChannelUnavailable, merge.TemplateRefused) as error:
            logger.warning(
                "Reminder %s not queued for lease %s: %s",
                rule.code,
                lease.pk,
                type(error).__name__,
            )
            return None

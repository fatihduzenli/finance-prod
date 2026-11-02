"""Admin › Outbox › Preview next 30 days (owner, 2026-11-01): what the daily reminder job
would queue on each of the coming days, read from today's data, queuing nothing.

**The rows.** For each day ``d`` from today, :func:`preview` asks the job's own planner,
:func:`ledger.services.reminders.plan`, with ``today=d``: so the preview and the job
(:func:`ledger.services.reminders.queue_due`) can never disagree. Each planned message is one
:class:`Row`: the day, the rule, the template, the store, the operator, the charge (or the rent
month), the amount and the outcome (would queue, held for want of a billing e-mail, paused by a
promise, or skipped and why). No e-mail address and no message text is shown. A message the
job's idempotency keeps once (per lease, rule and month; per charge, rule and occurrence) is
listed once, on its first day. Today's data is read as it stands: a rent charge made on its
day does not exist yet, so rent is announced by rules A and B by lease and month (E4).

**Groups** (owner, 2026-11-02). The rows are grouped as the job sends them: per day and lease,
the operator's rules A to D go as one message (:data:`ledger.services.reminders.COMBINED_RULES`,
its subject the most urgent's), and an overdue row within three days of the lease's last overdue
message waits for the fold (:func:`ledger.services.reminders.fold`, applied day by day from the
lease's last overdue message on record, and the messages already waiting from the Outbox). Each
:class:`Group` is one send: its rows (the charges, with due date and days late), the total, and
"Would send combined" or "Waits for the 3-day fold until <day>"; a group of one reads as its
row ("Would queue"). Every other row (E, paused, skipped) is a group of its own.

**Rule F** follows a returned debit and cannot be predicted: no F row is invented. The
automatic re-debits whose date falls in the window are listed instead (:class:`Redebit`).

**Read only.** Everything runs in one transaction that is always rolled back, so a helper that
wrote would leave nothing: no message, no job, no audit row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from finance.dates import NEW_YORK, day_text
from ledger.models import (
    MessageRule,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    RetryState,
)
from ledger.services import messaging, reminders

#: The page's window, in days from today (today included).
DAYS = 30
#: The most days the command takes.
MAX_DAYS = 90

RENT_BEFORE_ITS_CHARGE = (
    "Rent charges are made on their day and do not exist yet: rules A and B announce rent "
    "by lease and month, three days before its debit."
)
F_NOT_PREDICTABLE = "F follows a return; not predictable."
WOULD_SEND_COMBINED = "Would send combined"
WAITS_FOR_FOLD = "Waits for the 3-day fold until {day}"
WOULD_SEND_AFTER_FOLD = "Would send after the 3-day fold"
CHARGES_TEXT = "{count} charges: {names}"


@dataclass(frozen=True, slots=True)
class Row:
    """One message the job would plan on ``day`` (no address, no text)."""

    day: date
    rule: str
    rule_label: str
    template: str
    store_id: uuid.UUID | None
    store: str
    operator: str
    charge: str
    amount: Decimal
    outcome: str
    queues: bool
    #: The :class:`Group` (its number) the row goes in; its charge's due date (a lease month's
    #: debit date) and days late on ``day``.
    group: int = 0
    due: date | None = None
    days_late: int = 0


@dataclass(frozen=True, slots=True)
class Group:
    """One send of the job on ``day``: its first row's rule, template, store and operator,
    every row it lists, their total and the outcome."""

    number: int
    rows: list[Row]
    outcome: str

    @property
    def first(self) -> Row:
        return self.rows[0]

    @property
    def combined(self) -> bool:
        return len(self.rows) > 1

    @property
    def amount(self) -> Decimal:
        return sum((row.amount for row in self.rows), Decimal("0"))

    @property
    def charges(self) -> str:
        """ "2 charges: Rent November 2026, Broken shelf"; the charge alone for one."""
        if not self.combined:
            return self.first.charge
        names = ", ".join(row.charge for row in self.rows)
        return CHARGES_TEXT.format(count=len(self.rows), names=names)


@dataclass(frozen=True, slots=True)
class Redebit:
    """An automatic re-debit due in the window: rule F's message follows only a return."""

    day: date
    store_id: uuid.UUID | None
    store: str
    operator: str
    charge: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Preview:
    first: date
    last: date
    rows: list[Row]
    redebits: list[Redebit]
    groups: list[Group] = field(default_factory=list)


def _store_text(number: int | None, label: str) -> str:
    return f"#{number} {label}" if number is not None else label


def _row(item: reminders.Planned, day: date | None = None) -> Row:
    """The row of ``item``, on ``day`` (its own day unless given: a waiting message's is the
    day it goes)."""
    on = day or item.day
    lease = item.lease
    store_id: uuid.UUID | None = None
    store = ""
    if lease is not None:
        store_id = lease.store_id
        store = _store_text(lease.store.store_number, str(lease.store.external_label))
    elif item.row is not None:
        store_id, store = item.row.store_id, item.row.store_label
    if item.subject is not None:
        charge = messaging.charge_name(item.subject)
    else:
        charge = item.row.description if item.row is not None else ""
    if item.operator is not None:
        operator = str(item.operator.display_name)
    else:
        operator = item.row.operator_name if item.row is not None else ""
    due = messaging.due_of(item.subject) if item.subject is not None else None
    return Row(
        day=on,
        rule=str(item.rule.code),
        rule_label=MessageRule(item.rule.code).label,
        template=item.template.display_name if item.template is not None else item.rule.template,
        store_id=store_id,
        store=store,
        operator=operator,
        charge=charge,
        amount=item.amount,
        outcome=item.outcome_text,
        queues=item.queues,
        due=due,
        days_late=max((on - due).days, 0) if due is not None else 0,
    )


def _redebits(first: date, last: date) -> list[Redebit]:
    """The automatic re-debits, pending or scheduled, dated from ``first`` to ``last``."""
    found = (
        OneOffCharge.objects.filter(
            category=OneOffCategory.REDEBIT,
            retry_state__in=[RetryState.AUTO_PENDING, RetryState.SCHEDULED],
            retry_date__gte=first,
            retry_date__lte=last,
        )
        .exclude(status=OneOffChargeStatus.VOIDED)
        .select_related("operator", "fee_type", "lease__store")
        .order_by("retry_date", "created_at", "id")
    )
    rows: list[Redebit] = []
    for charge in found:
        lease = charge.lease
        if charge.retry_date is None:  # excluded by the filter
            continue
        rows.append(
            Redebit(
                day=charge.retry_date,
                store_id=lease.store_id if lease is not None else None,
                store=(
                    _store_text(lease.store.store_number, str(lease.store.external_label))
                    if lease is not None
                    else ""
                ),
                operator=str(charge.operator.display_name) if charge.operator else "",
                charge=charge.label,
                amount=Decimal(str(charge.balance)),
            )
        )
    return rows


def _rule_of(item: reminders.Planned) -> str:
    return str(item.rule.code)


def _already_waiting() -> tuple[dict[uuid.UUID, list[reminders.Planned]], dict[uuid.UUID, date]]:
    """The messages waiting for the fold in the Outbox, as planned items per lease, and the
    day each lease's go (a message whose charge is closed now is left out: it is dropped on
    that day)."""
    found = reminders.waiting()
    charges = messaging.charges_of(found)
    items: dict[uuid.UUID, list[reminders.Planned]] = {}
    days: dict[uuid.UUID, date] = {}
    for message in found:
        charge = charges.get(message.pk)
        if message.lease_id is None or message.scheduled_for is None or charge is None:
            continue
        amount = messaging.balance_of(charge)
        if amount <= 0:
            continue
        day = message.scheduled_for.astimezone(NEW_YORK).date()
        rule = reminders.Rule(message.rule, message.template.code, message.occurrence)
        items.setdefault(message.lease_id, []).append(
            reminders.Planned(
                day,
                rule,
                charge,
                None,
                message.lease,
                message.operator,
                message.template,
                amount,
                reminders.QUEUE,
            )
        )
        days[message.lease_id] = min(day, days.get(message.lease_id, day))
    return items, days


class _Grouper:
    """The job's sends, day after day (the module docstring): the lease's last overdue day and
    what waits for the fold are carried from one day to the next."""

    def __init__(self, today: date) -> None:
        self.last = reminders.last_overdue(today)
        self.pending, self.pending_from = _already_waiting()
        self.groups: list[Group] = []

    def _add(self, rows: list[Row], outcome: str) -> None:
        number = len(self.groups) + 1
        self.groups.append(
            Group(number, [replace(row, group=number, outcome=outcome) for row in rows], outcome)
        )

    def single(self, item: reminders.Planned) -> None:
        self._add([_row(item)], item.outcome_text)

    def lease_day(self, lease_id: uuid.UUID, items: list[reminders.Planned], day: date) -> None:
        going, held, until = reminders.fold(items, _rule_of, self.last.get(lease_id), day)
        released: list[reminders.Planned] = []
        if until is None:
            released = self.pending.pop(lease_id, [])
            self.pending_from.pop(lease_id, None)
        if held and until is not None:
            self.pending.setdefault(lease_id, []).extend(held)
            self.pending_from.setdefault(lease_id, until)
            self._add([_row(item) for item in held], WAITS_FOR_FOLD.format(day=day_text(until)))
        # A waiting message goes on this day, but its row was queued on its own day.
        sends = [(item, _row(item, day)) for item in going] + [
            (item, replace(_row(item, day), queues=False)) for item in released
        ]
        if not sends:
            return
        sends.sort(key=lambda pair: reminders.urgency(_rule_of(pair[0])))
        going = [item for item, _ in sends]
        if any(_rule_of(item) in reminders.OVERDUE_RULES for item in going):
            self.last[lease_id] = day
        rows = [row for _, row in sends]
        if len(rows) == 1:
            self._add(rows, WOULD_SEND_AFTER_FOLD if released else going[0].outcome_text)
            return
        text = WOULD_SEND_COMBINED
        if going[0].no_email:
            text = f"{text} ({reminders.HELD_NO_EMAIL})"
        self._add(rows, text)

    def day(self, items: list[reminders.Planned], day: date) -> None:
        by_lease: dict[uuid.UUID, list[reminders.Planned]] = {}
        for item in items:
            lease = item.lease
            if item.queues and lease is not None and _rule_of(item) in reminders.COMBINED_RULES:
                by_lease.setdefault(lease.pk, []).append(item)
            else:
                self.single(item)
        due = [lease for lease, first in self.pending_from.items() if first <= day]
        for lease_id in [*by_lease, *(lease for lease in due if lease not in by_lease)]:
            self.lease_day(lease_id, by_lease.get(lease_id, []), day)


def preview(today: date, days: int = DAYS) -> Preview:
    """The planner's messages for each of ``days`` days from ``today`` (see the module
    docstring), each kept once and grouped as the job would send them, and the automatic
    re-debits in the window; writes nothing."""
    last = today + timedelta(days=days - 1)
    seen: set[tuple[object, ...]] = set()
    with transaction.atomic():
        grouper = _Grouper(today)
        for offset in range(days):
            day = today + timedelta(days=offset)
            fresh: list[reminders.Planned] = []
            for item in reminders.plan(day):
                if item.key in seen:
                    continue
                seen.add(item.key)
                fresh.append(item)
            grouper.day(fresh, day)
        redebits = _redebits(today, last)
        transaction.set_rollback(True)
    rows = [row for group in grouper.groups for row in group.rows]
    return Preview(today, last, rows, redebits, grouper.groups)


def _cell(value: object) -> str:
    """One tab-separated cell: no tab or line break inside it."""
    return " ".join(str(value).split())


def as_lines(found: Preview) -> list[str]:
    """The command's text: a header, one tab-separated line per row with its group's number
    (the rows of one send share it), then the re-debits (no group)."""
    lines = [
        "\t".join(
            ("day", "rule", "template", "store", "operator", "charge", "amount", "outcome", "group")
        )
    ]
    for row in found.rows:
        cells = (
            row.day.isoformat(),
            row.rule,
            row.template,
            row.store,
            row.operator,
            row.charge,
            f"{row.amount:.2f}",
            row.outcome,
            str(row.group),
        )
        lines.append("\t".join(_cell(cell) for cell in cells))
    for redebit in found.redebits:
        cells = (
            redebit.day.isoformat(),
            "F",
            "automatic re-debit",
            redebit.store,
            redebit.operator,
            redebit.charge,
            f"{redebit.amount:.2f}",
            F_NOT_PREDICTABLE,
            "",
        )
        lines.append("\t".join(_cell(cell) for cell in cells))
    return lines

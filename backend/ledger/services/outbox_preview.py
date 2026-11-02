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

**Rule F** follows a returned debit and cannot be predicted: no F row is invented. The
automatic re-debits whose date falls in the window are listed instead (:class:`Redebit`).

**Read only.** Everything runs in one transaction that is always rolled back, so a helper that
wrote would leave nothing: no message, no job, no audit row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

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


def _store_text(number: int | None, label: str) -> str:
    return f"#{number} {label}" if number is not None else label


def _row(item: reminders.Planned) -> Row:
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
    return Row(
        day=item.day,
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


def preview(today: date, days: int = DAYS) -> Preview:
    """The planner's messages for each of ``days`` days from ``today`` (see the module
    docstring), each kept once, and the automatic re-debits in the window; writes nothing."""
    last = today + timedelta(days=days - 1)
    seen: set[tuple[object, ...]] = set()
    rows: list[Row] = []
    with transaction.atomic():
        for offset in range(days):
            for item in reminders.plan(today + timedelta(days=offset)):
                if item.key in seen:
                    continue
                seen.add(item.key)
                rows.append(_row(item))
        redebits = _redebits(today, last)
        transaction.set_rollback(True)
    return Preview(today, last, rows, redebits)


def _cell(value: object) -> str:
    """One tab-separated cell: no tab or line break inside it."""
    return " ".join(str(value).split())


def as_lines(found: Preview) -> list[str]:
    """The command's text: a header, one tab-separated line per row, then the re-debits."""
    lines = [
        "\t".join(("day", "rule", "template", "store", "operator", "charge", "amount", "outcome"))
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
        )
        lines.append("\t".join(_cell(cell) for cell in cells))
    return lines

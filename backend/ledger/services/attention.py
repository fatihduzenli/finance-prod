"""What needs someone's attention today (docs/qa/PLAN-011b D6).

Read only. :func:`attention` returns :class:`Item` rows of these kinds, each with a
``severity`` (``high``, ``medium``, ``low``), a one-line ``text``, the ``url`` of the
page that deals with it and ``since``, the day it began:

- ``overdue``: an open charge due before today (:func:`ledger.services.receivables.open_balances`),
  most days late first; ``high`` from :data:`OVERDUE_HIGH_DAYS` days late;
- ``returned_debit``: a returned ACH debit not rescheduled, with the text
  ``"Returned debit not rescheduled: <store>, <amount>, returned <date>"``. Rescheduling
  arrives in a later batch (PLAN-011b D5) and resolves the return; until then every
  return the returns queue still holds (``status = returned``, ``resolved_at`` null) is
  listed: ``payments.resolution_kind`` does not exist yet, so no return counts as
  rescheduled;
- ``installment_returned`` (PLAN-012e D11): one item per installment plan while one of
  its installments has a returned payment not resolved (the plan's status is then
  ``attention``), ``"Installment returned: <store>, <parent>, plan <n> of <N> paid"``,
  linking to the plan; cleared once the return is resolved (rescheduled with the NSF
  fee, or resolved with a note);
- ``lease_end_balance`` (PLAN-012e D14): one item per confirmed lease end whose
  "Lease-end balance" charge is still open (not paid, waived or voided, a balance above
  zero), ``"Lease-end balance: <operator> on store <label>, <amount> open since
  <ended_on>"``, with ``" (transfer, allowed by <name>)"`` when the lease end's transfer
  had the balance allowed by an admin; linking to the charge;
- ``broken_promise``: a promise to pay past its date (the rule is in
  :mod:`ledger.services.contacts`); a promise about a lease names its store and what is
  still open on that lease (PLAN-012 D3: ``"... : Acme on store 2477: owes $1,250.00,
  not kept"``); one without a lease names the operator only;
- ``insurance_due``: an insurance anniversary in the next :data:`INSURANCE_DAYS` days
  (the lease that holds the store that day), and ``insurance_premium_missing``: an
  insurance charge still waiting for its premium, or such an anniversary whose policy
  year has no premium yet;
- ``lease_ending``: a lease not ended whose ``ended_on`` is today or in the next
  :data:`LEASE_END_DAYS` days;
- ``landlord_lease_ending``: a store's live cost row in effect today whose
  ``landlord_lease_end`` is today or in the next :data:`LANDLORD_END_DAYS` days;
- ``store_no_open_date``: an active or prospective store without an open date;
- ``operator_no_email``: an operator, not archived, without an e-mail address;
- ``vacant_store``: an active store with no lease in effect today (collection ``active``
  or ``paused``, started on or before today, not ended before today) whose last lease
  ended more than :data:`VACANT_DAYS` days ago, or that never had a lease (prospective
  ones left out) and opened more than :data:`VACANT_DAYS` days ago;
- ``schedule_review``: a collection schedule, not ended, flagged after a lease change;
- ``dead_letters`` and ``reconciliation_mismatches``: one item each with the count, when
  there are any;
- ``calendar_today``: each calendar entry dated today.

Each kind lists at most ``limit`` items (the dashboard's :data:`KIND_LIMIT`); what is left
out is counted in :attr:`Attention.more`. With ``operator``, only the items of that
operator are returned (the operator page's banners): charges billed to it, its returns,
promises, leases and schedules, itself without an e-mail, and today's entries naming it
or its lease; store-level kinds and the system-wide counts are left out.

Bounded: a fixed number of queries whatever the number of rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Min, Q
from django.urls import reverse
from django.utils import timezone

from ledger.models import (
    ZERO,
    CollectionSchedule,
    CollectionStatus,
    ContactEntry,
    InstallmentPlan,
    InsuranceCharge,
    InsurancePremium,
    Lease,
    LeaseEnd,
    LeaseEndStatus,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentStatus,
    ScheduleState,
    Store,
    StoreCost,
    StoreStatus,
)
from ledger.services import calendar, contacts, installments, insurance, receivables, returns
from ledger.services.leases import format_amount
from payments.models import ReconciliationMismatch, WebhookEvent, WebhookOutcome

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
_SEVERITY_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}

OVERDUE = "overdue"
RETURNED_DEBIT = "returned_debit"
INSTALLMENT_RETURNED = "installment_returned"
LEASE_END_BALANCE = "lease_end_balance"
BROKEN_PROMISE = "broken_promise"
INSURANCE_DUE = "insurance_due"
INSURANCE_PREMIUM_MISSING = "insurance_premium_missing"
LEASE_ENDING = "lease_ending"
LANDLORD_LEASE_ENDING = "landlord_lease_ending"
STORE_NO_OPEN_DATE = "store_no_open_date"
OPERATOR_NO_EMAIL = "operator_no_email"
VACANT_STORE = "vacant_store"
SCHEDULE_REVIEW = "schedule_review"
DEAD_LETTERS = "dead_letters"
RECONCILIATION_MISMATCHES = "reconciliation_mismatches"
CALENDAR_TODAY = "calendar_today"
#: Every kind, in the order the list shows them within a severity.
KINDS: tuple[str, ...] = (
    OVERDUE,
    RETURNED_DEBIT,
    INSTALLMENT_RETURNED,
    LEASE_END_BALANCE,
    BROKEN_PROMISE,
    INSURANCE_PREMIUM_MISSING,
    INSURANCE_DUE,
    LEASE_ENDING,
    LANDLORD_LEASE_ENDING,
    SCHEDULE_REVIEW,
    DEAD_LETTERS,
    RECONCILIATION_MISMATCHES,
    VACANT_STORE,
    STORE_NO_OPEN_DATE,
    OPERATOR_NO_EMAIL,
    CALENDAR_TODAY,
)
KIND_LABELS: dict[str, str] = {
    OVERDUE: "overdue charges",
    RETURNED_DEBIT: "returned debits",
    INSTALLMENT_RETURNED: "installment plans with a returned debit",
    LEASE_END_BALANCE: "lease-end balances open",
    BROKEN_PROMISE: "broken promises",
    INSURANCE_PREMIUM_MISSING: "insurance premiums missing",
    INSURANCE_DUE: "insurance due soon",
    LEASE_ENDING: "leases ending",
    LANDLORD_LEASE_ENDING: "landlord leases ending",
    SCHEDULE_REVIEW: "schedules to review",
    DEAD_LETTERS: "dead-letter webhooks",
    RECONCILIATION_MISMATCHES: "reconciliation mismatches",
    VACANT_STORE: "vacant stores",
    STORE_NO_OPEN_DATE: "stores without an open date",
    OPERATOR_NO_EMAIL: "operators without an e-mail",
    CALENDAR_TODAY: "calendar entries today",
}

#: An overdue charge this many days late or more is ``high``.
OVERDUE_HIGH_DAYS = 30
INSURANCE_DAYS = 30
LEASE_END_DAYS = 60
LANDLORD_END_DAYS = 90
VACANT_DAYS = 30
#: Most items of one kind the dashboard lists.
KIND_LIMIT = 25
RETURNED_TEXT = "Returned debit not rescheduled: {store}, {amount}, returned {date}"
INSTALLMENT_RETURNED_TEXT = "Installment returned: {store}, {parent}, plan {paid} of {count} paid"
LEASE_END_BALANCE_TEXT = (
    "Lease-end balance: {operator} on store {label}, {amount} open since {ended_on}"
)
LEASE_END_BALANCE_ALLOWED = " (transfer, allowed by {name})"
#: A charge in one of these states owes nothing more.
_SETTLED_CHARGE = (OneOffChargeStatus.PAID, OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED)


@dataclass(frozen=True, slots=True)
class Item:
    kind: str
    severity: str
    text: str
    url: str
    since: date | None
    operator_id: uuid.UUID | None = None
    #: A broken promise's entry: its Kept, Reschedule and Broken actions (PLAN-012c-b D2).
    promise_id: uuid.UUID | None = None


@dataclass(slots=True)
class Attention:
    items: list[Item] = field(default_factory=list)
    #: Kind -> items left out by the per-kind limit.
    more: dict[str, int] = field(default_factory=dict)

    @property
    def more_lines(self) -> list[tuple[str, int]]:
        return [(KIND_LABELS[kind], count) for kind, count in self.more.items() if count]


# --- Kinds ---------------------------------------------------------------------------------


def _overdue(today: date, balances: list[receivables.OpenBalance]) -> list[Item]:
    rows = [row for row in balances if row.overdue(today)]
    rows.sort(key=lambda row: (row.due_on, row.store_label, str(row.charge_id)))
    found = []
    for row in rows:
        days = (today - row.due_on).days
        where = row.store_label or row.operator_name or "No store"
        if row.kind == receivables.KIND_RENT:
            url = reverse("ledger:charges:detail", kwargs={"pk": row.charge_id})
        elif row.kind == receivables.KIND_ONE_OFF:
            url = reverse("ledger:one_off_charges:detail", kwargs={"pk": row.charge_id})
        elif row.store_id is not None:
            url = reverse("ledger:stores:detail", kwargs={"pk": row.store_id})
        else:
            url = reverse("ledger:arrears:page")
        found.append(
            Item(
                kind=OVERDUE,
                severity=HIGH if days >= OVERDUE_HIGH_DAYS else MEDIUM,
                text=(
                    f"Overdue {days} day{'s' if days != 1 else ''}: {where}, {row.description}, "
                    f"{format_amount(row.balance)}"
                ),
                url=url,
                since=row.due_on,
                operator_id=row.operator_id,
            )
        )
    return found


def _returned(today: date, operator: uuid.UUID | None) -> list[Item]:
    found = []
    for payment in returns.open_returns():
        row = returns.row(payment)
        operator_id = row.operator.pk if row.operator is not None else None
        if operator is not None and operator_id != operator:
            continue
        returned_on = timezone.localdate(row.returned_at) if row.returned_at is not None else None
        found.append(
            Item(
                kind=RETURNED_DEBIT,
                severity=HIGH,
                text=RETURNED_TEXT.format(
                    store=row.store.external_label if row.store is not None else "No store",
                    amount=format_amount(row.amount),
                    date=returned_on.isoformat() if returned_on is not None else "unknown",
                ),
                url=f"{reverse('ledger:returns:list')}#return-{payment.pk}",
                since=returned_on,
                operator_id=operator_id,
            )
        )
    return found


def _installments_returned(operator: uuid.UUID | None) -> list[Item]:
    """One item per plan with a returned installment payment not resolved, earliest
    return first (three queries)."""
    returns_open = Payment.objects.filter(
        status=PaymentStatus.RETURNED,
        is_refund=False,
        resolved_at__isnull=True,
        one_off_charge__plan__isnull=False,
    )
    if operator is not None:
        returns_open = returns_open.filter(one_off_charge__plan__operator=operator)
    since: dict[uuid.UUID, date | None] = {}
    for plan_id, returned_at in returns_open.values_list(
        "one_off_charge__plan", "returned_at"
    ).order_by("returned_at", "id"):
        day = timezone.localdate(returned_at) if returned_at is not None else None
        since.setdefault(plan_id, day)
    plans = (
        InstallmentPlan.objects.filter(pk__in=list(since))
        .select_related("lease__store", "rent_charge", "one_off_charge__fee_type")
        .annotate(
            paid=Count("installments", filter=Q(installments__status=OneOffChargeStatus.PAID))
        )
    )
    by_pk = {plan.pk: plan for plan in plans}
    found = []
    for plan_id, day in since.items():
        plan = by_pk[plan_id]
        store = plan.lease.store.external_label if plan.lease is not None else "No store"
        found.append(
            Item(
                kind=INSTALLMENT_RETURNED,
                severity=HIGH,
                text=INSTALLMENT_RETURNED_TEXT.format(
                    store=store,
                    parent=installments.parent_label(plan.parent),
                    paid=plan.paid,
                    count=plan.count,
                ),
                url=reverse("ledger:plans:detail", kwargs={"pk": plan.pk}),
                since=day,
                operator_id=plan.operator_id,
            )
        )
    return found


def _lease_end_balances(operator: uuid.UUID | None) -> list[Item]:
    """One item per confirmed lease end whose lease-end balance charge is open, earliest
    end first (one query)."""
    rows = (
        LeaseEnd.objects.filter(
            status=LeaseEndStatus.CONFIRMED,
            debt_charge__isnull=False,
            debt_charge__balance__gt=0,
        )
        .exclude(debt_charge__status__in=_SETTLED_CHARGE)
        .select_related(
            "lease__store",
            "debt_charge__operator",
            "transfer__debt_override_by",
        )
        .order_by("ended_on", "id")
    )
    if operator is not None:
        rows = rows.filter(debt_charge__operator=operator)
    found = []
    for end in rows:
        charge = end.debt_charge
        if charge is None:
            continue
        text = LEASE_END_BALANCE_TEXT.format(
            operator=charge.operator.display_name if charge.operator is not None else "No operator",
            label=end.lease.store.external_label,
            amount=format_amount(charge.balance),
            ended_on=end.ended_on,
        )
        held_by = getattr(end, "transfer", None)
        if held_by is not None and held_by.debt_override_at is not None:
            by = held_by.debt_override_by
            text += LEASE_END_BALANCE_ALLOWED.format(name=by.get_username() if by else "unknown")
        found.append(
            Item(
                kind=LEASE_END_BALANCE,
                severity=HIGH,
                text=text,
                url=reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk}),
                since=end.ended_on,
                operator_id=charge.operator_id,
            )
        )
    return found


def _promise_text(promise: ContactEntry, owed: dict[uuid.UUID, Decimal]) -> str:
    """PLAN-012 D3: a promise on a lease names its store and that lease's open balance."""
    where = promise.operator.display_name
    if promise.lease is not None:
        balance = owed.get(promise.lease.pk, ZERO)
        where += f" on store {promise.lease.store.external_label}: owes {format_amount(balance)}"
    amount = format_amount(promise.promised_amount or ZERO)
    return f"Promise to pay {amount} by {promise.promised_date}: {where}, not kept"


def _promises(
    today: date, operator: uuid.UUID | None, balances: list[receivables.OpenBalance]
) -> list[Item]:
    owed: dict[uuid.UUID, Decimal] = {}
    for row in balances:
        if row.lease_id is not None:
            owed[row.lease_id] = owed.get(row.lease_id, ZERO) + row.balance
    return [
        Item(
            kind=BROKEN_PROMISE,
            severity=HIGH,
            text=_promise_text(promise, owed),
            url=_operator_url(promise.operator_id, "#contacts"),
            since=promise.promised_date,
            operator_id=promise.operator_id,
            promise_id=promise.pk,
        )
        for promise in contacts.broken_promises(today, operator=operator)
        if promise.promised_amount is not None
    ]


def _operator_url(operator_id: uuid.UUID, anchor: str = "") -> str:
    return f"{reverse('ledger:operators:detail', kwargs={'pk': operator_id})}{anchor}"


def _leases(operator: uuid.UUID | None) -> list[Lease]:
    rows = Lease.objects.select_related("store", "operator").exclude(
        collection_status=CollectionStatus.PROSPECTIVE
    )
    if operator is not None:
        rows = rows.filter(operator=operator)
    return list(rows.order_by("store__external_label", "id"))


def _lease_url(lease: Lease) -> str:
    return reverse("ledger:leases:detail", kwargs={"pk": lease.pk})


def _where(lease: Lease) -> str:
    label = lease.store.external_label
    return f"{label} ({lease.operator.display_name})" if lease.operator is not None else label


def _insurance(today: date, leases: list[Lease]) -> list[Item]:
    end = today + timedelta(days=INSURANCE_DAYS)
    premiums = set(InsurancePremium.objects.values_list("policy_year", flat=True))
    missing = {
        (lease_id, year): due
        for lease_id, year, due in InsuranceCharge.objects.filter(
            needs_premium=True, lease__in=[lease.pk for lease in leases]
        ).values_list("lease_id", "policy_year", "due_on")
    }
    found = []
    seen: set[tuple[uuid.UUID, int]] = set()
    for lease in leases:
        store = lease.store
        if store.open_date is None:
            continue
        for year in sorted({today.year, end.year}):
            if year < store.open_date.year:
                continue
            due = insurance.anniversary(store.open_date, year)
            if not today <= due <= end or not insurance.holds_on(lease, due):
                continue
            if store.closed_at is not None and due >= store.closed_at:
                continue
            no_premium = year not in premiums
            seen.add((lease.pk, year))
            found.append(
                Item(
                    kind=INSURANCE_PREMIUM_MISSING if no_premium else INSURANCE_DUE,
                    severity=MEDIUM if no_premium else LOW,
                    text=(
                        f"Insurance premium for {year} missing, due {due}: {_where(lease)}"
                        if no_premium
                        else f"Insurance due {due}: {_where(lease)}"
                    ),
                    url=_lease_url(lease),
                    since=due,
                    operator_id=lease.operator_id,
                )
            )
    by_pk = {lease.pk: lease for lease in leases}
    for (lease_id, year), due in sorted(missing.items(), key=lambda pair: pair[1]):
        holder = by_pk.get(lease_id)
        if holder is None or (lease_id, year) in seen:
            continue
        found.append(
            Item(
                kind=INSURANCE_PREMIUM_MISSING,
                severity=MEDIUM,
                text=f"Insurance premium for {year} missing, due {due}: {_where(holder)}",
                url=_lease_url(holder),
                since=due,
                operator_id=holder.operator_id,
            )
        )
    return found


def _lease_ending(today: date, leases: list[Lease]) -> list[Item]:
    end = today + timedelta(days=LEASE_END_DAYS)
    return [
        Item(
            kind=LEASE_ENDING,
            severity=MEDIUM,
            text=f"Lease ends {lease.ended_on}: {_where(lease)}",
            url=_lease_url(lease),
            since=lease.ended_on,
            operator_id=lease.operator_id,
        )
        for lease in sorted(leases, key=lambda lease: (lease.ended_on or today, str(lease.pk)))
        if lease.ended_on is not None
        and today <= lease.ended_on <= end
        and lease.collection_status != CollectionStatus.ENDED
    ]


def _schedules(operator: uuid.UUID | None) -> list[Item]:
    rows = (
        CollectionSchedule.objects.filter(needs_review=True)
        .exclude(state=ScheduleState.ENDED)
        .select_related("lease__store", "lease__operator")
        .order_by("updated_at", "id")
    )
    if operator is not None:
        rows = rows.filter(lease__operator=operator)
    return [
        Item(
            kind=SCHEDULE_REVIEW,
            severity=MEDIUM,
            text=(
                f"{schedule.get_kind_display()} schedule to review after a lease change: "
                f"{_where(schedule.lease)}"
            ),
            url=_lease_url(schedule.lease),
            since=timezone.localdate(schedule.updated_at),
            operator_id=schedule.lease.operator_id,
        )
        for schedule in rows
    ]


def _landlord(today: date) -> list[Item]:
    end = today + timedelta(days=LANDLORD_END_DAYS)
    rows = (
        StoreCost.objects.filter(
            superseded_at__isnull=True,
            effective_from__lte=today,
            landlord_lease_end__gte=today,
            landlord_lease_end__lte=end,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
        .select_related("store")
        .order_by("landlord_lease_end", "id")
    )
    return [
        Item(
            kind=LANDLORD_LEASE_ENDING,
            severity=MEDIUM,
            text=f"Landlord lease ends {row.landlord_lease_end}: {row.store.external_label}",
            url=reverse("ledger:stores:detail", kwargs={"pk": row.store_id}),
            since=row.landlord_lease_end,
        )
        for row in rows
    ]


def _stores_without_open_date() -> list[Item]:
    rows = Store.objects.filter(
        open_date__isnull=True, status__in=[StoreStatus.ACTIVE, StoreStatus.PROSPECTIVE]
    ).order_by("external_label", "id")
    return [
        Item(
            kind=STORE_NO_OPEN_DATE,
            severity=LOW,
            text=f"Store without an open date: {store.external_label}",
            url=reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
            since=None,
        )
        for store in rows
    ]


def _operators_without_email(operator: uuid.UUID | None) -> list[Item]:
    rows = Operator.objects.filter(email__isnull=True, archived_at__isnull=True)
    if operator is not None:
        rows = rows.filter(pk=operator)
    return [
        Item(
            kind=OPERATOR_NO_EMAIL,
            severity=LOW,
            text=f"Operator without an e-mail address: {row.display_name}",
            url=_operator_url(row.pk),
            since=None,
            operator_id=row.pk,
        )
        for row in rows.order_by("display_name", "id")
    ]


def _in_effect(lease: Lease, today: date) -> bool:
    return (
        lease.collection_status in (CollectionStatus.ACTIVE, CollectionStatus.PAUSED)
        and (lease.started_on is None or lease.started_on <= today)
        and (lease.ended_on is None or lease.ended_on >= today)
    )


def _vacant(today: date) -> list[Item]:
    stores = (
        Store.objects.filter(status=StoreStatus.ACTIVE)
        .prefetch_related("leases")
        .order_by("external_label", "id")
    )
    found = []
    for store in stores:
        leases = [
            lease
            for lease in store.leases.all()
            if lease.collection_status != CollectionStatus.PROSPECTIVE
        ]
        if any(_in_effect(lease, today) for lease in leases):
            continue
        ended = [lease.ended_on for lease in leases if lease.ended_on is not None]
        if ended:
            since = max(ended) + timedelta(days=1)
        elif not leases and store.open_date is not None:
            since = store.open_date
        else:
            continue
        if (today - since).days < VACANT_DAYS:
            continue
        found.append(
            Item(
                kind=VACANT_STORE,
                severity=LOW,
                text=f"Vacant since {since}: {store.external_label}",
                url=reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
                since=since,
            )
        )
    return found


def _system_counts() -> list[Item]:
    found = []
    dead = WebhookEvent.objects.filter(outcome=WebhookOutcome.DEAD_LETTER).aggregate(
        count=Count("id"), first=Min("received_at")
    )
    if dead["count"]:
        found.append(
            Item(
                kind=DEAD_LETTERS,
                severity=HIGH,
                text=f"Dead-letter webhook events: {dead['count']}",
                url=f"{reverse('ledger:webhooks:list')}?outcome={WebhookOutcome.DEAD_LETTER}",
                since=timezone.localdate(dead["first"]),
            )
        )
    open_mismatches = ReconciliationMismatch.objects.filter(resolved_at__isnull=True).aggregate(
        count=Count("id"), first=Min("created_at")
    )
    if open_mismatches["count"]:
        found.append(
            Item(
                kind=RECONCILIATION_MISMATCHES,
                severity=MEDIUM,
                text=f"Unresolved reconciliation mismatches: {open_mismatches['count']}",
                url=reverse("ledger:reconciliation:page"),
                since=timezone.localdate(open_mismatches["first"]),
            )
        )
    return found


def _calendar(today: date, operator: uuid.UUID | None) -> list[Item]:
    return [
        Item(
            kind=CALENDAR_TODAY,
            severity=LOW,
            text=f"{item.kind_label} today: {item.title}",
            url=item.url,
            since=today,
            operator_id=item.operator_id,
        )
        for item in calendar.today_entries(today)
        if operator is None or item.operator_id == operator
    ]


# --- The list ------------------------------------------------------------------------------


def _limited(items: Iterable[Item], limit: int | None, more: dict[str, int]) -> list[Item]:
    """The first ``limit`` items of each kind, in their order; the rest counted in ``more``."""
    kept: list[Item] = []
    seen: dict[str, int] = {}
    for item in items:
        seen[item.kind] = seen.get(item.kind, 0) + 1
        if limit is None or seen[item.kind] <= limit:
            kept.append(item)
        else:
            more[item.kind] = more.get(item.kind, 0) + 1
    return kept


def attention(
    today: date, *, operator: uuid.UUID | None = None, limit: int | None = KIND_LIMIT
) -> Attention:
    """Every item for ``today`` (see the module docstring), by severity, then kind, then
    the order within the kind; with ``operator``, only that operator's items."""
    leases = _leases(operator)
    balances = receivables.open_balances(today, operator=operator)
    sources: list[Callable[[], list[Item]]] = [
        lambda: _overdue(today, balances),
        lambda: _returned(today, operator),
        lambda: _installments_returned(operator),
        lambda: _lease_end_balances(operator),
        lambda: _promises(today, operator, balances),
        lambda: _insurance(today, leases),
        lambda: _lease_ending(today, leases),
        lambda: _schedules(operator),
        lambda: _operators_without_email(operator),
        lambda: _calendar(today, operator),
    ]
    if operator is None:
        sources += [
            lambda: _landlord(today),
            _stores_without_open_date,
            lambda: _vacant(today),
            _system_counts,
        ]
    result = Attention()
    found = [item for source in sources for item in _limited(source(), limit, result.more)]
    order = {kind: index for index, kind in enumerate(KINDS)}
    # A stable sort: within a kind the source's own order stays (overdue: most days late
    # first).
    found.sort(key=lambda item: (_SEVERITY_RANK[item.severity], order[item.kind]))
    result.items = found
    return result

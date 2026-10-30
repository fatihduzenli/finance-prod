"""Stores created and edited by staff (docs/qa/PLAN-007b-stores.md §1).

Views call the functions below; nothing here talks to the payment provider. Every
change writes one ``audit_log`` row (``store.created``, ``store.updated``).

A store has no column for the one-time key its create form carries, so the key is
recorded in the ``store.created`` audit row, as for leases. Store creation has no parent
row to lock while that row is looked up, so it takes a lock on one ``sync_state`` row
(:data:`CREATION_LOCK`) for the length of the transaction: two submissions of one form
make one store.

The New store form also carries the store's first cost row (PLAN-012e-2 D9): the landlord
rent (``0.00`` is a known "no cost"), the landlord lease end and the other monthly cost
with its description. :func:`create_store` writes that row through
:func:`ledger.services.store_costs.set_cost`, in the store's transaction and with the same
``store_cost.created`` audit row as "Change" writes, effective from the open date (today
without one); a row the cost service refuses saves no store either (:class:`CostRefused`).

:func:`address_line` is the store's address on one line and :func:`status_badge` the
badge status of its status (D7). A store's ``state`` is a two-letter code (PLAN-012f-b D11):
:func:`state_text` is what a screen shows (the full name), :func:`state_choices` a filter's
choices; CSV exports keep the code.

:func:`store_list` reads the stores list (PLAN-012f-a D3; DESIGN-SPEC §4 Stores list): one row
per store with its current lease's operator, rent and channel, its open balance as a badge
(:func:`ledger.services.receivables.open_balances` grouped by store) and its status (Closed
for a closed or terminated store, Vacant without a lease in effect or to come, else Active);
the counts line (every store, those with a lease, the rest vacant); the filters ``q``,
``state``, ``status``, ``balance`` and ``sort``, each checked against its fixed list. Read
only.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Case, Exists, F, OuterRef, Q, Subquery, Value, When
from django.utils import timezone

from finance.us_states import state_name
from ledger.models import (
    AuditLog,
    CollectionStatus,
    Lease,
    LeaseRentHistory,
    PaymentChannel,
    RentChargeStatus,
    Store,
    StoreCostSource,
    StoreStatus,
)
from ledger.services import receivables, search, store_costs
from ledger.wording import format_amount
from payments.models import SyncState

ENTITY_STORE = "store"
#: The ``sync_state`` row locked while a store is created.
CREATION_LOCK = "store_creation_lock"
#: The store fields staff edit, in form order.
STORE_FIELDS: tuple[str, ...] = (
    "store_number",
    "external_label",
    "store_type",
    "address_line1",
    "city",
    "state",
    "postal_code",
    "open_date",
    "closed_at",
    "status",
)
#: What :func:`address_line` shows when the store has no address part at all.
NO_ADDRESS = "\u2014"
#: The badge status (``finance.templatetags.components``) of each store status; a status
#: not named here (Prospective) is a plain badge.
STATUS_BADGES: dict[str, str] = {
    StoreStatus.ACTIVE: "paid",
    StoreStatus.CLOSED: "attention",
    StoreStatus.TERMINATED: "attention",
}


class CostRefused(Exception):  # noqa: N818 (reads as an outcome, like FileRefused)
    """The cost service refused the New store form's first cost row; nothing was saved."""

    def __init__(self, outcome: store_costs.CostOutcome) -> None:
        super().__init__(outcome.message)
        self.outcome = outcome


def state_text(code: str) -> str:
    """A store's state as a screen shows it: the full name of its code ("TX" is "Texas"),
    the stored value itself when it is not a code (nothing is hidden), "" for none."""
    cleaned = " ".join(str(code or "").split())
    return state_name(cleaned) or cleaned


def state_choices(codes: Iterable[str]) -> list[tuple[str, str]]:
    """(code, name) of each distinct non-empty code, sorted by name: a State filter's
    choices."""
    found = {code: state_text(code) for code in codes if code}
    return sorted(found.items(), key=lambda item: (item[1].casefold(), item[0]))


def address_line(store: Store) -> str:
    """Street, city and "state ZIP" joined with ", " (the state by its full name); missing
    parts are left out, and "\u2014" stands for an address with no part at all."""
    street, city, postal = (
        " ".join(str(part or "").split())
        for part in (store.address_line1, store.city, store.postal_code)
    )
    state = state_text(store.state)
    region = " ".join(part for part in (state, postal) if part)
    return ", ".join(part for part in (street, city, region) if part) or NO_ADDRESS


def title(store: Store) -> str:
    """The store's title (PLAN-012e-4 D4, the grid's store cell; the store page's heading,
    PLAN-012f D8): "#1991 Istanbul". A name that starts with the number (then a space or a
    hyphen, or nothing) shows the rest once ("2922 TUCSON" is "#2922 TUCSON"); a name that is
    only the number (the sheet's imported stores) shows the city after it, if any ("#2475
    CONYERS", else "#2475"); a store without a number shows its name only."""
    return title_of(store.store_number, store.external_label, store.city)


def title_of(number: int | None, name: str, city: str = "") -> str:
    """:func:`title` from the values a query read (the P&L's rows, PLAN-012f-b E1-1)."""
    if number is None:
        return name
    rest = re.match(rf"{number}(?!\d)[ -]?(.*)\Z", name, re.S)
    if rest is not None:
        name = rest.group(1).strip() or city.strip()
    return f"#{number} {name}" if name else f"#{number}"


def crumb(store: Store) -> str:
    """The store in a breadcrumb (PLAN-012f D8): "#1991", or its name without a number."""
    return f"#{store.store_number}" if store.store_number is not None else store.external_label


def status_badge(store: Store) -> str:
    """The badge status of the store's status ("" for a plain badge)."""
    return STATUS_BADGES.get(store.status, "")


def store_for_creation_key(raw: object) -> Store | None:
    """The store a create form with this key already made, if any."""
    try:
        key = uuid.UUID(str(raw))
    except ValueError:
        return None
    return _by_creation_key(key)


def create_store(
    fields: Mapping[str, Any], *, creation_key: uuid.UUID, actor: User | None
) -> tuple[Store, bool]:
    """Save a new store, or return the store this key already made.

    Returns ``(store, created)``. A replayed key never changes the existing row,
    whatever fields came with it.
    """
    with transaction.atomic():
        SyncState.objects.get_or_create(job_name=CREATION_LOCK)
        SyncState.objects.select_for_update().get(job_name=CREATION_LOCK)
        existing = _by_creation_key(creation_key)
        if existing is not None:
            return existing, False
        store = Store(**_editable(fields))
        store.save()
        _audit(
            actor,
            "store.created",
            store,
            None,
            {**_snapshot(store), "creation_key": str(creation_key)},
        )
        if fields.get("landlord_rent") is not None:
            _first_cost(store, fields, actor)
    return store, True


def _first_cost(store: Store, fields: Mapping[str, Any], actor: User | None) -> None:
    """The store's first cost row, from its open date (today without one); raises
    :class:`CostRefused`, which rolls the store back, when the cost service refuses it."""
    outcome = store_costs.set_cost(
        store,
        fields["landlord_rent"],
        store.open_date or timezone.localdate(),
        other_monthly_cost=fields.get("other_monthly_cost"),
        other_cost_description=fields.get("other_cost_description") or "",
        landlord_lease_end=fields.get("landlord_lease_end"),
        source=StoreCostSource.STAFF,
        actor=actor,
    )
    if not outcome.ok:
        raise CostRefused(outcome)


def update_store(store_id: uuid.UUID, fields: Mapping[str, Any], *, actor: User | None) -> Store:
    """Apply staff edits under a row lock; an edit that changes nothing writes no audit row."""
    with transaction.atomic():
        store = Store.objects.select_for_update().get(pk=store_id)
        before = _snapshot(store)
        for name, value in _editable(fields).items():
            setattr(store, name, value)
        store.save()
        after = _snapshot(store)
        if after != before:
            _audit(actor, "store.updated", store, before, after)
    return store


def _by_creation_key(creation_key: uuid.UUID) -> Store | None:
    entry = AuditLog.objects.filter(
        action="store.created",
        entity_type=ENTITY_STORE,
        after__creation_key=str(creation_key),
    ).first()
    if entry is None:
        return None
    return Store.objects.filter(pk=entry.entity_id).first()


def _editable(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {name: fields[name] for name in STORE_FIELDS if name in fields}


def _snapshot(store: Store) -> dict[str, Any]:
    return {
        "store_number": store.store_number,
        "external_label": store.external_label,
        "store_type": str(store.store_type_id) if store.store_type_id else None,
        "address_line1": store.address_line1,
        "city": store.city,
        "state": store.state,
        "postal_code": store.postal_code,
        "open_date": store.open_date.isoformat() if store.open_date else None,
        "closed_at": store.closed_at.isoformat() if store.closed_at else None,
        "status": store.status,
        "needs_open_date": store.needs_open_date,
    }


def _audit(
    actor: User | None,
    action: str,
    store: Store,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_STORE,
        entity_id=str(store.pk),
        before=before,
        after=after,
    )


# --- The stores list (PLAN-012f-a D3) ---------------------------------------------------------

#: The Status filter's values and words; "" (All) is the default.
LIST_STATUS_ACTIVE = "active"
LIST_STATUS_VACANT = "vacant"
LIST_STATUS_CLOSED = "closed"
LIST_STATUSES: tuple[tuple[str, str], ...] = (
    ("", "All"),
    (LIST_STATUS_ACTIVE, "Active"),
    (LIST_STATUS_VACANT, "Vacant"),
    (LIST_STATUS_CLOSED, "Closed"),
)
#: The Balance filter's values and words; "" (Any) is the default.
BALANCE_OWING = "owing"
BALANCE_PAID_UP = "paid"
BALANCE_OVERDUE = "overdue"
LIST_BALANCES: tuple[tuple[str, str], ...] = (
    ("", "Any"),
    (BALANCE_OWING, "Owing"),
    (BALANCE_PAID_UP, "Paid up"),
    (BALANCE_OVERDUE, "Overdue"),
)
#: The Sort's values and words; the first is the default.
SORT_NUMBER = "number"
SORT_NAME = "name"
SORT_BALANCE = "balance"
LIST_SORTS: tuple[tuple[str, str], ...] = (
    (SORT_NUMBER, "Store number"),
    (SORT_NAME, "Name"),
    (SORT_BALANCE, "Balance"),
)
#: The store statuses the list shows as closed (the Closed filter and badge).
CLOSED_STATUSES: tuple[str, ...] = (StoreStatus.CLOSED, StoreStatus.TERMINATED)


@dataclass(frozen=True, slots=True)
class ListFilter:
    """The stores list's filters, each a value of its fixed list (an unknown value is the
    default, never kept)."""

    query: str = ""
    state: str = ""
    status: str = ""
    balance: str = ""
    sort: str = SORT_NUMBER

    @property
    def narrowed(self) -> bool:
        """A filter other than the sort is set: the footer says "filtered from"."""
        return bool(self.query or self.state or self.status or self.balance)

    def as_query(self) -> dict[str, str]:
        """The filters that differ from their default, for the pager's links."""
        found = {
            "q": self.query,
            "state": self.state,
            "status": self.status,
            "balance": self.balance,
            "sort": "" if self.sort == SORT_NUMBER else self.sort,
        }
        return {key: value for key, value in found.items() if value}


def _one_of(raw: object, allowed: Iterable[str]) -> str:
    value = raw if isinstance(raw, str) else ""
    return value if value in set(allowed) else ""


def parse_list_filter(params: Mapping[str, str], states: Iterable[str]) -> ListFilter:
    """The filters of ``params`` (a request's GET): ``q`` cleaned as the search cleans it;
    ``state`` one of ``states``; ``status``, ``balance`` and ``sort`` from their lists."""
    return ListFilter(
        query=search.clean_query(params.get("q")),
        state=_one_of(params.get("state"), states),
        status=_one_of(params.get("status"), (value for value, _ in LIST_STATUSES)),
        balance=_one_of(params.get("balance"), (value for value, _ in LIST_BALANCES)),
        sort=_one_of(params.get("sort"), (value for value, _ in LIST_SORTS)) or SORT_NUMBER,
    )


@dataclass(frozen=True, slots=True)
class ListBalance:
    """A store's open balance as its badge reads it: the text and the badge value
    (``finance.templatetags.components``)."""

    amount: Decimal
    overdue: bool
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class ListRow:
    store: Store
    title: str
    #: "City, State" by the state's full name (either alone when the other is missing; ""
    #: without both).
    place: str
    operator_id: uuid.UUID | None
    operator_name: str
    rent: Decimal | None
    #: The current lease's payment channel as the lease page words it; "" without a lease.
    channel: str
    has_lease: bool
    #: ``active``, ``vacant``, ``closed`` or the store's own status (Prospective).
    status_key: str
    status_label: str
    status_value: str
    balance: ListBalance


@dataclass(frozen=True, slots=True)
class StoreList:
    rows: list[ListRow]
    filters: ListFilter
    #: Every store, before any filter; those with a lease in effect or to come.
    total: int
    with_lease: int
    #: (code, name) of the distinct states of every store, by name: the State filter's
    #: choices.
    states: list[tuple[str, str]]

    @property
    def vacant(self) -> int:
        return self.total - self.with_lease


def _place(store: Store) -> str:
    return ", ".join(part for part in (store.city.strip(), state_text(store.state)) if part)


def _list_stores(query: str, today: date) -> list[Store]:
    """Every store in one query, each annotated with its current lease (in effect today, else
    the latest started of those not ended before today), that lease's operator, channel and
    rent in effect today, the day its last lease ended, and whether it matches ``query``."""
    not_ended = Q(ended_on__isnull=True) | Q(ended_on__gte=today)
    in_effect_now = Q(collection_status=CollectionStatus.ACTIVE) & (
        Q(started_on__isnull=True) | Q(started_on__lte=today)
    )
    current = (
        Lease.objects.filter(not_ended, store=OuterRef("pk"))
        .annotate(
            list_rank=Case(When(in_effect_now, then=Value(0)), default=Value(1)),
        )
        .order_by("list_rank", F("started_on").desc(nulls_last=True), "-created_at")
    )
    rent = (
        LeaseRentHistory.objects.filter(lease=OuterRef("list_lease"), effective_from__lte=today)
        .exclude(effective_to__lt=today)
        .order_by("-effective_from")
    )
    last_ended = Lease.objects.filter(store=OuterRef("pk"), ended_on__isnull=False).order_by(
        "-ended_on"
    )
    stores = (
        Store.objects.select_related("store_type")
        .annotate(
            list_lease=Subquery(current.values("pk")[:1]),
            list_operator=Subquery(current.values("operator")[:1]),
            list_operator_name=Subquery(current.values("operator__display_name")[:1]),
            list_channel=Subquery(current.values("payment_channel")[:1]),
            list_last_ended=Subquery(last_ended.values("ended_on")[:1]),
        )
        .annotate(list_rent=Subquery(rent.values("amount")[:1]))
    )
    if search.words(query):
        stores = stores.annotate(
            list_matched=Exists(
                search.filter_stores(Store.objects.filter(pk=OuterRef("pk")), query, today)
            )
        )
    return list(stores.order_by())


def _store_balances(today: date) -> dict[uuid.UUID, tuple[Decimal, bool, bool]]:
    """Per store: what its open charges owe (:func:`ledger.services.receivables.open_balances`,
    the one place that decides it), whether one is overdue and whether one is partly paid."""
    found: dict[uuid.UUID, tuple[Decimal, bool, bool]] = {}
    for row in receivables.open_balances(today):
        if row.store_id is None:
            continue
        amount, overdue, partial = found.get(row.store_id, (Decimal("0.00"), False, False))
        found[row.store_id] = (
            amount + row.balance,
            overdue or row.overdue(today),
            partial or row.status == RentChargeStatus.PARTIAL,
        )
    return found


def _vacant_label(last_ended: date | None, today: date) -> str:
    """The Status of a store without a lease (PLAN-014 A4): "Vacant · 23 days" since its last
    lease ended, or "Vacant · never leased" when it never had one (a lease not ended before
    today is the store's current lease, so a vacant store's leases all ended)."""
    if last_ended is None:
        return "Vacant · never leased"
    days = max((today - last_ended).days, 0)
    return f"Vacant · {days} day{'s' if days != 1 else ''}"


def _balance(owed: tuple[Decimal, bool, bool] | None, has_lease: bool) -> ListBalance:
    """The Balance badge: an open balance by its worst state (overdue, partly paid, unpaid);
    nothing owed reads "Paid up" with a lease. Without a lease (PLAN-014 A4) the previous
    lease's leftover reads "$1,200.00 unpaid" and nothing owed has no badge (the page's "—")."""
    amount, overdue, partial = owed or (Decimal("0.00"), False, False)
    if not has_lease:
        if amount > 0:
            return ListBalance(amount, overdue, f"{format_amount(amount)} unpaid", "due")
        return ListBalance(amount, False, "", "")
    if amount > 0:
        text = format_amount(amount)
        if overdue:
            return ListBalance(amount, True, f"{text} overdue", "overdue")
        if partial:
            return ListBalance(amount, False, f"{text} partly paid", "partial")
        return ListBalance(amount, False, f"{text} unpaid", "due")
    return ListBalance(amount, False, "Paid up", "paid")


def _status(
    store: Store, has_lease: bool, last_ended: date | None, today: date
) -> tuple[str, str, str]:
    """(key, label, badge value) of the Status column; ``last_ended`` is the day the store's
    last lease ended (the list's ``list_last_ended``), for a vacant store's label."""
    if store.status in CLOSED_STATUSES:
        return LIST_STATUS_CLOSED, store.get_status_display(), status_badge(store)
    if not has_lease:
        return LIST_STATUS_VACANT, _vacant_label(last_ended, today), "neutral"
    if store.status == StoreStatus.ACTIVE:
        return LIST_STATUS_ACTIVE, "Active", status_badge(store)
    return store.status, store.get_status_display(), "neutral"


def _row(store: Any, owed: tuple[Decimal, bool, bool] | None, today: date) -> ListRow:
    has_lease = store.list_lease is not None
    key, label, value = _status(store, has_lease, store.list_last_ended, today)
    channel = store.list_channel or ""
    return ListRow(
        store=store,
        title=title(store),
        place=_place(store),
        # A subquery's UUID comes back as text on SQLite; the same UUID on PostgreSQL.
        operator_id=uuid.UUID(str(store.list_operator)) if store.list_operator else None,
        operator_name=store.list_operator_name or "",
        rent=store.list_rent,
        channel=str(PaymentChannel(channel).label) if channel in PaymentChannel.values else "",
        has_lease=has_lease,
        status_key=key,
        status_label=label,
        status_value=value,
        balance=_balance(owed, has_lease),
    )


def _kept(row: ListRow, filters: ListFilter) -> bool:
    if filters.state and row.store.state != filters.state:
        return False
    if filters.status and row.status_key != filters.status:
        return False
    if filters.balance == BALANCE_OWING:
        return row.balance.amount > 0
    if filters.balance == BALANCE_OVERDUE:
        return row.balance.overdue
    if filters.balance == BALANCE_PAID_UP:
        return row.balance.amount == 0 and row.has_lease
    return True


def _number_key(row: ListRow) -> tuple[object, ...]:
    number = row.store.store_number
    return (number is None, number or 0, row.store.external_label.lower(), str(row.store.pk))


def _sort_key(sort: str) -> Callable[[ListRow], tuple[object, ...]]:
    if sort == SORT_NAME:
        return lambda row: (row.store.external_label.lower(), str(row.store.pk))
    if sort == SORT_BALANCE:
        return lambda row: (-row.balance.amount, *_number_key(row))
    return _number_key


def store_list(params: Mapping[str, str], today: date) -> StoreList:
    """The stores list (PLAN-012f-a D3) in five queries whatever the number of stores: every
    store with its current lease, operator, channel and rent (one), and the open balances
    (:func:`ledger.services.receivables.open_balances`, four; six when a re-debit carries a
    charge). The counts and the states are of every store; the filters, the Balance among
    them, apply to every store before the sort, never to one page."""
    stores = _list_stores(search.clean_query(params.get("q")), today)
    states = state_choices(store.state for store in stores)
    filters = parse_list_filter(params, (code for code, _ in states))
    balances = _store_balances(today)
    rows = [
        _row(store, balances.get(store.pk), today)
        for store in stores
        if not filters.query or getattr(store, "list_matched", True)
    ]
    kept = sorted((row for row in rows if _kept(row, filters)), key=_sort_key(filters.sort))
    return StoreList(
        rows=kept,
        filters=filters,
        total=len(stores),
        with_lease=sum(1 for store in stores if getattr(store, "list_lease", None) is not None),
        states=states,
    )

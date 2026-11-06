"""Search over stores and operators (docs/qa/PLAN-012d-store-workspace.md D16, D17).

Read only: nothing here writes or calls the payment provider.

**The picker** (:func:`store_rows`, PLAN-012f-b D7): the same store match, at most
:data:`PICKER_MAX` rows for the dashboard's "Which store?" dialog, each linking to the
store's Create invoice page.

**The query** (:func:`clean_query`): the ``q`` text without control characters, cut to
:data:`QUERY_MAX_LENGTH` characters before any use, surrounding spaces removed. An empty
or whitespace-only query is no filter. :func:`words` splits it on whitespace (a word
repeated, in any case, counts once).

**The calendar entry's link** (:func:`link_rows`, owner 2026-11-06): the New calendar entry
dialog's one search over stores, operators (not archived) and leases, at most
:data:`LINK_MAX` of each, one query each.

**A store matches** (:func:`filter_stores`) when every word matches one of: its store
number as a prefix (``24`` finds 2477; a leading ``#`` is allowed; digits only), its
name, its city, or the display name of the operator of one of its leases in effect or to
come (not ended before today); the text matches are case-insensitive "contains". **An
operator matches** (:func:`filter_operators`) when every word is in its display name (the
Full name, PLAN-014 C1) or its e-mail address, as the operators list matches, archived
ones included (the search lists them and never jumps to them).

Only ORM filters: the words reach SQL as bound parameters, never as SQL text. Each filter
adds conditions to the one query it is given; it never adds a query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from django.db.models import CharField, Exists, F, OuterRef, Q, QuerySet
from django.db.models.functions import Cast, Lower
from django.urls import reverse
from django.utils import timezone

from finance.dates import day_text
from ledger.models import Lease, Operator, Store
from ledger.services.collections import mask_address

#: The longest query used, in characters (D16); the rest is dropped before any use.
QUERY_MAX_LENGTH = 100
#: The most stores and operators the results page lists, each (D17).
RESULTS_MAX = 50

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_NUMBER = re.compile(r"#?([0-9]+)")


def clean_query(raw: str | None) -> str:
    """``raw`` without control characters, cut to :data:`QUERY_MAX_LENGTH`, stripped."""
    return _CONTROL_CHARACTERS.sub("", (raw or "")[:QUERY_MAX_LENGTH]).strip()


def words(query: str) -> list[str]:
    """The query's words, each once (case ignored), in their order."""
    seen: set[str] = set()
    found: list[str] = []
    for word in query.split():
        key = word.casefold()
        if key not in seen:
            seen.add(key)
            found.append(word)
    return found


def number_prefix(word: str) -> str | None:
    """The digits a word matches a store number by, as a prefix (``#24`` and ``24`` give
    ``"24"``); ``None`` when the word is not a number."""
    number = _NUMBER.fullmatch(word)
    return number.group(1) if number is not None else None


def _store_word(word: str, today: date) -> Q:
    operator_named = Exists(
        Lease.objects.filter(store=OuterRef("pk"), operator__display_name__icontains=word).filter(
            Q(ended_on__isnull=True) | Q(ended_on__gte=today)
        )
    )
    condition = Q(external_label__icontains=word) | Q(city__icontains=word) | Q(operator_named)
    number = number_prefix(word)
    if number is not None:
        condition |= Q(_search_number__startswith=number)
    return condition


def filter_stores(
    stores: QuerySet[Store], query: str, today: date | None = None
) -> QuerySet[Store]:
    """``stores`` narrowed to those matching every word of ``query`` (see the module
    docstring); ``stores`` unchanged when the query has no word."""
    found = words(query)
    if not found:
        return stores
    day = today or timezone.localdate()
    narrowed = stores.annotate(_search_number=Cast("store_number", CharField()))
    for word in found:
        narrowed = narrowed.filter(_store_word(word, day))
    return narrowed


def filter_operators(operators: QuerySet[Operator], query: str) -> QuerySet[Operator]:
    """``operators`` narrowed to those whose display name or e-mail address holds each word
    of ``query``; unchanged when the query has no word. The first and last name columns no
    form writes since PLAN-014 C1 are not searched."""
    for word in words(query):
        operators = operators.filter(Q(display_name__icontains=word) | Q(email__icontains=word))
    return operators


# --- The top-bar search (D17) -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoreHit:
    store: Store
    operator: Operator | None


@dataclass(frozen=True, slots=True)
class Results:
    query: str
    stores: list[StoreHit]
    operators: list[Operator]
    #: More stores or operators matched than :data:`RESULTS_MAX`.
    more_stores: bool
    more_operators: bool

    @property
    def empty(self) -> bool:
        return not self.stores and not self.operators

    @property
    def jump(self) -> Store | Operator | None:
        """The one page to go to: the only store when no operator matched, or the only
        operator, not archived, when no store matched; else ``None``."""
        if len(self.stores) == 1 and not self.operators and not self.more_stores:
            return self.stores[0].store
        if (
            len(self.operators) == 1
            and not self.stores
            and not self.more_operators
            and self.operators[0].archived_at is None
        ):
            return self.operators[0]
        return None


def _current_operator(store: Store, today: date) -> Operator | None:
    """The grid's row operator (:func:`ledger.services.grid.current_lease`), from the
    prefetched leases."""
    # Imported here: the grid imports this module for its filter.
    from ledger.services.grid import current_lease

    lease = current_lease(list(store.leases.all()), today)
    return lease.operator if lease is not None else None


def search(query: str, today: date | None = None) -> Results:
    """The stores and operators matching ``query`` (three queries: stores, their leases,
    operators); nothing when the query has no word."""
    day = today or timezone.localdate()
    if not words(query):
        return Results(query, [], [], False, False)
    stores = list(
        filter_stores(Store.objects.all(), query, day)
        .prefetch_related("leases__operator")
        .order_by(F("store_number").asc(nulls_last=True), Lower("external_label"), "id")[
            : RESULTS_MAX + 1
        ]
    )
    operators = list(
        filter_operators(Operator.objects.all(), query).order_by(Lower("display_name"), "id")[
            : RESULTS_MAX + 1
        ]
    )
    return Results(
        query,
        [StoreHit(store, _current_operator(store, day)) for store in stores[:RESULTS_MAX]],
        operators[:RESULTS_MAX],
        more_stores=len(stores) > RESULTS_MAX,
        more_operators=len(operators) > RESULTS_MAX,
    )


# --- The "Which store?" picker (PLAN-012f-b D7) ------------------------------------------

#: The most stores the picker lists (D7).
PICKER_MAX = 10


def store_rows(
    query: str, limit: int = PICKER_MAX, today: date | None = None
) -> list[dict[str, object]]:
    """The picker's rows for ``query`` (already cleaned): at most ``limit`` stores matching
    every word as the top-bar search matches them, each ``{"number", "name", "operator",
    "closed", "url"}`` with ``url`` the store's Create invoice page; ``[]`` when the query
    has no word. Two queries (the stores, their leases)."""
    # Imported here: the stores service imports this module.
    from ledger.services.stores import CLOSED_STATUSES

    day = today or timezone.localdate()
    if not words(query):
        return []
    stores = list(
        filter_stores(Store.objects.all(), query, day)
        .prefetch_related("leases__operator")
        .order_by(F("store_number").asc(nulls_last=True), Lower("external_label"), "id")[:limit]
    )
    rows: list[dict[str, object]] = []
    for store in stores:
        operator = _current_operator(store, day)
        rows.append(
            {
                "number": store.store_number,
                "name": store.external_label,
                "operator": operator.display_name if operator is not None else "",
                "closed": store.status in CLOSED_STATUSES,
                "url": reverse("ledger:invoices:new_store", kwargs={"pk": store.pk}),
            }
        )
    return rows


# --- The New calendar entry dialog's link (owner, 2026-11-06) ------------------------------

#: The most rows of each kind the link's type-ahead lists.
LINK_MAX = 8


def _store_label(number: int | None, name: str) -> str:
    return f"#{number} {name}" if number is not None else name


def _lease_word(word: str) -> Q:
    condition = Q(store__external_label__icontains=word) | Q(operator__display_name__icontains=word)
    number = number_prefix(word)
    if number is not None:
        condition |= Q(_search_number__startswith=number)
    return condition


def link_rows(query: str, limit: int = LINK_MAX) -> list[dict[str, str]]:
    """The link's rows for ``query`` (already cleaned): the stores matching every word as the
    top-bar search matches them, the operators not archived whose name or e-mail holds each
    word, and the leases whose store (name or number) or operator holds each word; at most
    ``limit`` of each, each ``{"kind", "id", "label", "sub"}``: a store's number and name over
    its city, an operator's name over its e-mail masked, a lease's store and start over its
    operator. ``[]`` when the query has no word. Three queries."""
    if not words(query):
        return []
    stores = filter_stores(Store.objects.all(), query).order_by(
        F("store_number").asc(nulls_last=True), Lower("external_label"), "id"
    )[:limit]
    operators = filter_operators(Operator.objects.filter(archived_at__isnull=True), query).order_by(
        Lower("display_name"), "id"
    )[:limit]
    leases = Lease.objects.select_related("store", "operator").annotate(
        _search_number=Cast("store__store_number", CharField())
    )
    for word in words(query):
        leases = leases.filter(_lease_word(word))
    rows: list[dict[str, str]] = [
        {
            "kind": "store",
            "id": str(store.pk),
            "label": _store_label(store.store_number, store.external_label),
            "sub": store.city,
        }
        for store in stores
    ]
    rows += [
        {
            "kind": "operator",
            "id": str(operator.pk),
            "label": operator.display_name,
            "sub": mask_address(operator.email) if operator.email else "",
        }
        for operator in operators
    ]
    rows += [
        {
            "kind": "lease",
            "id": str(lease.pk),
            "label": lease_label(lease),
            "sub": lease.operator.display_name if lease.operator is not None else "",
        }
        for lease in leases.order_by(Lower("store__external_label"), "started_on", "id")[:limit]
    ]
    return rows


def lease_label(lease: Lease) -> str:
    """A lease as the link names it: its store, then "from" its start ("no start date")."""
    started = day_text(lease.started_on) if lease.started_on else "no start date"
    return f"{_store_label(lease.store.store_number, lease.store.external_label)} from {started}"


def link_label(record: Store | Operator | Lease) -> str:
    """The chip's words for a linked record (the dialog shown again after a refusal)."""
    if isinstance(record, Lease):
        return lease_label(record)
    if isinstance(record, Store):
        return _store_label(record.store_number, record.external_label)
    return record.display_name

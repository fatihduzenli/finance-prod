"""Search over stores and operators (docs/qa/PLAN-012d-store-workspace.md D16, D17).

Read only: nothing here writes or calls the payment provider.

**The picker** (:func:`store_rows`, PLAN-012f-b D7): the same store match, at most
:data:`PICKER_MAX` rows for the dashboard's "Which store?" dialog, each linking to the
store's Create invoice page.

**The query** (:func:`clean_query`): the ``q`` text without control characters, cut to
:data:`QUERY_MAX_LENGTH` characters before any use, surrounding spaces removed. An empty
or whitespace-only query is no filter. :func:`words` splits it on whitespace (a word
repeated, in any case, counts once).

**A store matches** (:func:`filter_stores`) when every word matches one of: its store
number as a prefix (``24`` finds 2477; a leading ``#`` is allowed; digits only), its
name, its city, or the display name of the operator of one of its leases in effect or to
come (not ended before today); the text matches are case-insensitive "contains". **An
operator matches** (:func:`filter_operators`) when every word is in its display name,
archived ones included (the search lists them and never jumps to them).

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

from ledger.models import Lease, Operator, Store

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


def _store_word(word: str, today: date) -> Q:
    operator_named = Exists(
        Lease.objects.filter(store=OuterRef("pk"), operator__display_name__icontains=word).filter(
            Q(ended_on__isnull=True) | Q(ended_on__gte=today)
        )
    )
    condition = Q(external_label__icontains=word) | Q(city__icontains=word) | Q(operator_named)
    number = _NUMBER.fullmatch(word)
    if number is not None:
        condition |= Q(_search_number__startswith=number.group(1))
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
    """``operators`` narrowed to those whose display name holds every word of
    ``query``; unchanged when the query has no word."""
    for word in words(query):
        operators = operators.filter(display_name__icontains=word)
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

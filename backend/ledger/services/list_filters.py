"""The list pages' active filters
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md A5): what the "Filters · n"
button counts, the chips under the search field and the panel's Clear link, from the request's
query string and the fields each page declares once.

Read only, no query. A field counts when its value is not empty, not the page's default and,
when the page gives the field's choices, one of them (an unknown value is one the page's own
parser dropped: it filters nothing, so it has no chip). The search and the sort never count.
A chip's link is the same page without that parameter (and without ``page``: the rows change);
Clear keeps only the parameters named in ``keep`` (the search and the sort).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from django.http import HttpRequest, QueryDict

#: A field's chip label and its choices (value → words); ``None`` shows the value as written.
FieldSpec = tuple[str, Mapping[str, str] | None]

#: The pager's parameter, dropped by every link here.
PAGE_PARAM = "page"


@dataclass(frozen=True, slots=True)
class Chip:
    label: str
    value_label: str
    remove_url: str


@dataclass(frozen=True, slots=True)
class ActiveFilters:
    count: int
    chips: list[Chip]
    clear_url: str


@dataclass(frozen=True, slots=True)
class SortOption:
    label: str
    url: str
    current: bool


def _url(request: HttpRequest, params: QueryDict) -> str:
    query = params.urlencode()
    return f"{request.path}?{query}" if query else request.path


def _without(request: HttpRequest, names: Iterable[str]) -> QueryDict:
    params = request.GET.copy()
    for name in (*names, PAGE_PARAM):
        params.pop(name, None)
    return params


def active(
    request: HttpRequest,
    fields: Mapping[str, FieldSpec],
    *,
    defaults: Mapping[str, str] | None = None,
    keep: Iterable[str] = ("q", "sort"),
) -> ActiveFilters:
    """The active filters of ``request`` among ``fields``, in the fields' order."""
    chips: list[Chip] = []
    for name, (label, choices) in fields.items():
        value = (request.GET.get(name, "") or "").strip()
        if not value or value == (defaults or {}).get(name, ""):
            continue
        if choices is not None and value not in choices:
            continue
        words = choices[value] if choices is not None else value
        chips.append(Chip(label, words, _url(request, _without(request, (name,)))))
    kept = QueryDict(mutable=True)
    for name in keep:
        for value in request.GET.getlist(name):
            if value:
                kept.appendlist(name, value)
    return ActiveFilters(count=len(chips), chips=chips, clear_url=_url(request, kept))


def sort_menu(
    request: HttpRequest,
    choices: Iterable[tuple[str, str]],
    current: str,
    *,
    param: str = "sort",
    drop: Iterable[str] = (),
) -> list[SortOption]:
    """The Sort menu's options: each the same page with ``param`` set (``drop`` removed, the
    pager reset)."""
    options = []
    for value, label in choices:
        params = _without(request, (param, *drop))
        params[param] = value
        options.append(SortOption(label, _url(request, params), value == current))
    return options

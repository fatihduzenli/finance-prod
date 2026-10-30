"""The list pages' active filters
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md A5): what the "Filters · n"
button counts, the chips under the search field and the panel's Clear link, from the request's
query string and the fields each page declares once.

Read only, no query. Every link starts from ``accepted``, the parameters the page's own parser
kept (its filter's ``as_query()``), never from the request as written, so a value the parser
dropped appears in no link. A field counts when its value is not empty, not the page's default
and, when the page gives the field's choices, one of them (a value outside them filters nothing
known: it has no chip and is left out of every link too). The search and the sort never count.
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
    #: The page's accepted parameters without a field value outside its choices: what every
    #: link starts from (:func:`sort_menu` takes it).
    params: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SortOption:
    label: str
    url: str
    current: bool


def _url(request: HttpRequest, params: Mapping[str, str]) -> str:
    query = QueryDict(mutable=True)
    for name, value in params.items():
        query[name] = value
    encoded = query.urlencode()
    return f"{request.path}?{encoded}" if encoded else request.path


def _without(params: Mapping[str, str], names: Iterable[str]) -> dict[str, str]:
    dropped = {*names, PAGE_PARAM}
    return {name: value for name, value in params.items() if name not in dropped}


def active(
    request: HttpRequest,
    fields: Mapping[str, FieldSpec],
    *,
    accepted: Mapping[str, str],
    defaults: Mapping[str, str] | None = None,
    keep: Iterable[str] = ("q", "sort"),
) -> ActiveFilters:
    """The active filters among ``fields``, in the fields' order, from ``accepted`` (the page's
    parsed filter as query values)."""
    params = {
        name: value
        for name, value in accepted.items()
        if name not in fields or fields[name][1] is None or value in (fields[name][1] or {})
    }
    chips: list[Chip] = []
    for name, (label, choices) in fields.items():
        value = (params.get(name, "") or "").strip()
        if not value or value == (defaults or {}).get(name, ""):
            continue
        words = choices[value] if choices is not None else value
        chips.append(Chip(label, words, _url(request, _without(params, (name,)))))
    kept_names = set(keep)
    kept = {name: value for name, value in params.items() if name in kept_names and value}
    return ActiveFilters(
        count=len(chips), chips=chips, clear_url=_url(request, kept), params=params
    )


def sort_menu(
    request: HttpRequest,
    params: Mapping[str, str],
    choices: Iterable[tuple[str, str]],
    current: str,
    *,
    param: str = "sort",
    drop: Iterable[str] = (),
) -> list[SortOption]:
    """The Sort menu's options: each the same page, from ``params`` (an
    :attr:`ActiveFilters.params`), with ``param`` set (``drop`` removed, the pager reset)."""
    options = []
    for value, label in choices:
        option = _without(params, (param, *drop))
        option[param] = value
        options.append(SortOption(label, _url(request, option), value == current))
    return options

"""The list pages' search and filters
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md A5): one component for
Stores, Operators, the Monthly rent overview, Payments, Leases, Unpaid balances and the P&L.

A block tag rather than an include, because each page's filter fields differ (selects, a
checkbox, month pickers, hidden sort inputs): the page writes its fields, unchanged, between
the tags and the component puts them in the panel::

    {% filters active=active_filters search_name="q" search_value=query
       search_maxlength=100 search_placeholder="…" search_label="Search stores"
       sort_options=sort_options %}
      … the page's filter fields …
    {% endfilters %}

It renders ``finance/_filters.html``: ``<form method="get" id="filters" class="filters"
role="search" data-results="#results">`` with the search field (only when ``search_name`` is
given; ``search_id`` keeps a page's own id on it), the "Filters" button ("Filters · 2" from
``active``, a :class:`ledger.services.list_filters.ActiveFilters`), the "Sort: … ▾" menu when
``sort_options`` (:func:`ledger.services.list_filters.sort_menu`) is given, the panel
``#filters-panel`` with the fields and its Clear and Apply, and the chips row. ``action`` is
the form's address (the page's own path by default). ``finance/static/finance/filters.js``
toggles the panel and swaps ``#results`` as the search is typed.
"""

from __future__ import annotations

from typing import Any

from django import template
from django.template.base import FilterExpression, NodeList, Parser, Token
from django.template.context import Context
from django.utils.safestring import SafeString, mark_safe

register = template.Library()

TEMPLATE = "finance/_filters.html"
#: The arguments the tag takes; any other is a template error.
ARGUMENTS = frozenset(
    {
        "active",
        "action",
        "search_name",
        "search_value",
        "search_maxlength",
        "search_placeholder",
        "search_label",
        "search_id",
        "sort_options",
    }
)


class FiltersNode(template.Node):
    def __init__(self, nodelist: NodeList, arguments: dict[str, FilterExpression]) -> None:
        self.nodelist = nodelist
        self.arguments = arguments

    def render(self, context: Context) -> SafeString:
        values: dict[str, Any] = {
            name: expression.resolve(context) for name, expression in self.arguments.items()
        }
        request = context.get("request")
        if not values.get("action") and request is not None:
            values["action"] = request.path
        options = values.get("sort_options") or []
        values["sort_current"] = next((option.label for option in options if option.current), "")
        values["fields"] = mark_safe(self.nodelist.render(context))  # noqa: S308 (the page's own markup)
        component = context.template.engine.get_template(TEMPLATE) if context.template else None
        if component is None:
            raise template.TemplateSyntaxError("{% filters %} needs a template context")
        with context.push(filters_ui=values):
            return mark_safe(component.render(context))  # noqa: S308 (an escaping template)


@register.tag(name="filters")
def filters(parser: Parser, token: Token) -> FiltersNode:
    bits = token.split_contents()[1:]
    arguments: dict[str, FilterExpression] = {}
    for bit in bits:
        name, sep, value = bit.partition("=")
        if not sep or name not in ARGUMENTS:
            raise template.TemplateSyntaxError(f"{{% filters %}} does not take {bit!r}")
        arguments[name] = parser.compile_filter(value)
    nodelist = parser.parse(("endfilters",))
    parser.delete_first_token()
    return FiltersNode(nodelist, arguments)

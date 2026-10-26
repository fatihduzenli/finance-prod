"""The top-bar search (docs/qa/PLAN-012d-store-workspace.md D17).

``ledger:search`` (GET, any verified role; read-only in ``accounts.registry``): the
``q`` of the box in the main navigation, matched by :func:`ledger.services.search.search`
over every store and operator (every role sees both lists today). Exactly one store and
no operator: a redirect to the store page; exactly one operator, not archived, and no
store: a redirect to the operator page; otherwise the results page, with the query back
in its box, the stores (number, name, city, current operator) and the operators (name,
e-mail masked as the message log masks it, archived marked), or "No match". Nothing is
written.

``intent=new_invoice`` (PLAN-012f-b D7, the "Which store?" picker without scripts): the page
reads "New invoice — which store?", each store links to its Create invoice page, the one
store matched jumps there (never to an operator) and the box keeps the intent; for a viewer
the intent is ignored.

``ledger:search_stores`` (GET, billing and above; read-only in ``accounts.registry``): the
picker's type-ahead, ``{"stores": [...]}`` from :func:`ledger.services.search.store_rows`
for ``q`` (cleaned as the page cleans it; empty: no row), with ``<``, ``>`` and ``&`` written
as ``\\u`` escapes so that no markup appears in the body. Nothing is written.
"""

from __future__ import annotations

from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_safe

from accounts.roles import Role, meets, role_of
from accounts.views import forbidden
from ledger.models import Store
from ledger.services import search as service
from ledger.services.collections import mask_address
from ledger.views.operators import role_required

#: The ``intent`` of the picker's search (the scripts-off form and its fallback link).
NEW_INVOICE = "new_invoice"


@role_required
@require_safe
def search_page(request: HttpRequest) -> HttpResponse:
    query = service.clean_query(request.GET.get("q"))
    can_act = meets(role_of(request.user), Role.BILLING)
    # A viewer, who cannot open the builder, gets the plain search.
    new_invoice = can_act and request.GET.get("intent") == NEW_INVOICE
    results = service.search(query)
    target = results.jump
    if new_invoice:
        if len(results.stores) == 1 and not results.more_stores:
            return redirect("ledger:invoices:new_store", pk=results.stores[0].store.pk)
    elif isinstance(target, Store):
        return redirect("ledger:stores:detail", pk=target.pk)
    elif target is not None:
        return redirect("ledger:operators:detail", pk=target.pk)
    operators = [
        (operator, mask_address(operator.email) if operator.email else "")
        for operator in results.operators
    ]
    return render(
        request,
        "ledger/search/results.html",
        {
            "query": query,
            "results": results,
            "operators": operators,
            "new_invoice": new_invoice,
            "can_act": can_act,
        },
    )


#: The characters a JSON body never carries as such (json_script's escapes).
_JSON_ESCAPES = {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}


class _EscapedJSONEncoder(DjangoJSONEncoder):
    def encode(self, o: Any) -> str:
        return super().encode(o).translate(_JSON_ESCAPES)


@role_required
@require_safe
def stores_json(request: HttpRequest) -> HttpResponse:
    if not meets(role_of(request.user), Role.BILLING):
        return forbidden(request)
    query = service.clean_query(request.GET.get("q"))
    return JsonResponse({"stores": service.store_rows(query)}, encoder=_EscapedJSONEncoder)

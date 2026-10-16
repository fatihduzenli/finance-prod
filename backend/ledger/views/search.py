"""The top-bar search (docs/qa/PLAN-012d-store-workspace.md D17).

``ledger:search`` (GET, any verified role; read-only in ``accounts.registry``): the
``q`` of the box in the main navigation, matched by :func:`ledger.services.search.search`
over every store and operator (every role sees both lists today). Exactly one store and
no operator: a redirect to the store page; exactly one operator, not archived, and no
store: a redirect to the operator page; otherwise the results page, with the query back
in its box, the stores (number, name, city, current operator) and the operators (name,
e-mail masked as the message log masks it, archived marked), or "No match". Nothing is
written.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_safe

from ledger.models import Store
from ledger.services import search as service
from ledger.services.collections import mask_address
from ledger.views.operators import role_required


@role_required
@require_safe
def search_page(request: HttpRequest) -> HttpResponse:
    query = service.clean_query(request.GET.get("q"))
    results = service.search(query)
    target = results.jump
    if isinstance(target, Store):
        return redirect("ledger:stores:detail", pk=target.pk)
    if target is not None:
        return redirect("ledger:operators:detail", pk=target.pk)
    operators = [
        (operator, mask_address(operator.email) if operator.email else "")
        for operator in results.operators
    ]
    return render(
        request,
        "ledger/search/results.html",
        {"query": query, "results": results, "operators": operators},
    )

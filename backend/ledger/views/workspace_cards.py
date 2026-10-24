"""The store workspace's header lines and side cards (docs/qa/PLAN-012f-approved-design.md D8),
shared by the store page and the operator page's selected store.

:func:`header_lines` gives the page header's lines: the breadcrumb's store ("#1991", or the
name of a store without a number), the title ("#1991 Istanbul", ledger.services.stores.title),
the one line under it (the address and "Open since <date>", the parts that exist, joined by
" · ") and the phone's line (the operator and the city, the same way). :func:`side_cards` gives
what the side cards read besides the workspace header: the store's cost in effect (STORE COST),
its newest documents (DOCUMENTS, at most :data:`DOCUMENTS_SHOWN`, one query), the Documents
tab's link for Upload (billing, while document storage is configured) and the Autopay tab's
link for "Set up". Every value is text the template escapes.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.http import HttpRequest

from accounts.roles import Role, meets, role_of
from finance.dates import day_text
from ledger.models import Operator, Store
from ledger.services import documents as document_service
from ledger.services import store_costs, workspace
from ledger.services import stores as store_service

#: The newest documents the DOCUMENTS card lists; the Documents tab lists every one.
DOCUMENTS_SHOWN = 5


def _joined(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def header_lines(store: Store, operator: Operator | None) -> dict[str, str]:
    """The breadcrumb's store, the title, the address line and the phone's line."""
    address = store_service.address_line(store)
    opened = f"Open since {day_text(store.open_date)}" if store.open_date else ""
    place = ", ".join(part for part in (store.city.strip(), store.state.strip()) if part)
    return {
        "store_crumb": store_service.crumb(store),
        "store_title": store_service.title(store),
        "store_line": _joined("" if address == store_service.NO_ADDRESS else address, opened),
        "phone_line": _joined(operator.display_name if operator is not None else "", place),
    }


def side_cards(request: HttpRequest, store: Store, today: date) -> dict[str, Any]:
    """What the STORE COST and DOCUMENTS cards read, and the side cards' links."""
    from ledger.views.documents import feature_on  # ledger.views.documents imports views

    can_upload = meets(role_of(request.user), Role.BILLING) and feature_on()
    return {
        "cost": store_costs.current_cost(store, today),
        "side_documents": [
            (row, row.content_type.startswith("image/"))
            for row in document_service.documents_of(store)[:DOCUMENTS_SHOWN]
        ],
        "side_upload_url": (
            workspace.tab_url(store.pk, workspace.TAB_DOCUMENTS) if can_upload else ""
        ),
        "side_autopay_url": workspace.tab_url(store.pk, workspace.TAB_SCHEDULES),
        "can_set_cost": meets(role_of(request.user), Role.BILLING),
    }

"""The detail family's shared context (docs/qa/PLAN-012f-a-design-follow-up.md D4): the Activity
card's rows (:func:`ledger.services.detail_cards.activity`) and its "All activity" link, which
opens the store page's History tab (admin, as that tab is), or the audit page filtered to the
record for a record without a store (admin, as that page is); other roles get no link."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from django.urls import reverse

from ledger.models import Store
from ledger.services import audit
from ledger.services import detail_cards as service


def activity_context(
    records: Iterable[tuple[str, object]],
    *,
    admin: bool,
    store: Store | None,
    record: tuple[str, object],
) -> dict[str, Any]:
    """``activity`` (the rows of ``record`` and ``records``) and ``activity_url``."""
    url = ""
    if admin and store is not None:
        url = reverse("ledger:stores:detail", kwargs={"pk": store.pk}) + "?tab=history#tab-history"
    elif admin:
        kind, pk = record
        end = audit.today_in_new_york()
        query = {
            "entity_type": kind,
            "entity_id": str(pk),
            "from": (end - timedelta(days=audit.MAX_DAYS - 1)).isoformat(),
            "to": end.isoformat(),
        }
        url = reverse("ledger:audit:page") + "?" + urlencode(query)
    return {"activity": service.activity([record, *records]), "activity_url": url}

"""A form opened from the store page (docs/qa/PLAN-012d-store-workspace.md D2, D8).

The store page's action bar opens the existing forms with ``?store=<id>&lease=<id>``.
Such a form hides the fields those name (store, lease, operator), shows one read-only
line instead ("Store 2477 · Lease from 2026-10-13 · Operator Acme"), and after a
successful save returns to the store page's tab that shows the result
(:func:`ledger.services.workspace.tab_url`). The values are checked against the record
the form is for, never trusted: a ``store`` other than the form's store, or a ``lease``
that is not a lease of that store (or not the form's lease), answers 404, so a form is
never pre-filled across stores. A form opened from elsewhere returns to ``?next=`` when
it is a path on this site (:func:`local_next`), else where it returned before
(:func:`return_url`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

from django.http import Http404, HttpRequest
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from finance.dates import day_text
from ledger.models import Lease, Store
from ledger.services import workspace


@dataclass(frozen=True, slots=True)
class StoreContext:
    """The store (and lease) a form was opened from."""

    store: Store
    lease: Lease | None

    @property
    def line(self) -> str:
        """The read-only line shown in place of the hidden fields."""
        parts = [f"Store {self.store.external_label}"]
        if self.lease is not None:
            started = self.lease.started_on
            parts.append(f"Lease from {day_text(started)}" if started else "Lease")
            if self.lease.operator is not None:
                parts.append(f"Operator {self.lease.operator.display_name}")
        return " · ".join(parts)

    @property
    def query(self) -> str:
        """``?store=...&lease=...``: the query string that keeps the context."""
        values = {"store": str(self.store.pk)}
        if self.lease is not None:
            values["lease"] = str(self.lease.pk)
        return "?" + urlencode(values)

    def tab_url(self, tab: str) -> str:
        return workspace.tab_url(self.store.pk, tab)


def local_next(value: str | None) -> str:
    """``value`` when it is a path on this site (no scheme, no host, no ``//``), without
    its fragment; else ``""``."""
    target = (value or "").split("#", 1)[0]
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        return ""
    if not url_has_allowed_host_and_scheme(target, allowed_hosts=set()):
        return ""
    return target


def return_url(
    request: HttpRequest, context: StoreContext | None, tab: str | None, default: str
) -> str:
    """Where a saved form goes (D8): the store page's ``tab`` (its header when ``tab`` is
    ``None``) when opened from it; else ``?next=`` when it is a path on this site; else
    ``default``."""
    if context is not None:
        if tab is None:
            return reverse("ledger:stores:detail", kwargs={"pk": context.store.pk})
        return context.tab_url(tab)
    return local_next(request.POST.get("next") or request.GET.get("next")) or default


def _key(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise Http404 from None


def _params(request: HttpRequest) -> tuple[str, str]:
    source = request.GET
    return (source.get("store") or "").strip(), (source.get("lease") or "").strip()


def from_request(
    request: HttpRequest, *, store: Store, lease: Lease | None = None
) -> StoreContext | None:
    """The context of a form for ``store`` (and, for a lease's form, ``lease``) when the
    request names a store; ``None`` when it names neither a store nor a lease. 404 when
    the named store is not ``store``, or the named lease is not ``lease`` (a lease's
    form) or not a lease of ``store``."""
    raw_store, raw_lease = _params(request)
    if not raw_store and not raw_lease:
        return None
    if not raw_store or _key(raw_store) != store.pk:
        raise Http404
    if not raw_lease:
        return StoreContext(store=store, lease=lease)
    key = _key(raw_lease)
    if lease is not None:
        if key != lease.pk or lease.store_id != store.pk:
            raise Http404
        return StoreContext(store=store, lease=lease)
    named = Lease.objects.filter(pk=key, store=store).select_related("operator").first()
    if named is None:
        raise Http404
    return StoreContext(store=store, lease=named)


def for_lease(request: HttpRequest, lease: Lease) -> StoreContext | None:
    """:func:`from_request` for a form about one lease: the store is the lease's."""
    return from_request(request, store=lease.store, lease=lease)

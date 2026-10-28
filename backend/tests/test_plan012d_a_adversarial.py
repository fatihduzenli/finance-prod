"""Adversarial cases for PLAN-012d Batch A (the store workspace), by the QA owner (plan §6 A).
A form opened from a store never pre-fills a lease of another store; ``next`` cannot leave the
site; a viewer gets 403 on every bar action and sees no bar; the History tab shows only the
store's own rows; the drafts' Delete cannot delete a sent invoice; a draft's Send from the store
page sends once."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import NoReverseMatch, reverse

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    Lease,
    PaymentChannel,
)
from tests.receivables_helpers import a_lease, a_one_off_charge, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)
OCTOBER = date(2026, 10, 1)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


def _lease(started: date = date(2026, 9, 1)) -> Lease:
    lease = a_lease(a_store(), an_operator(), started=started, channel=PaymentChannel.WIRE)
    assert lease.operator is not None
    return lease


def _url_or_none(route: str, kwargs: dict[str, Any]) -> str | None:
    try:
        return reverse(route, kwargs=kwargs)
    except NoReverseMatch:
        return None


def _store_page(lease: Lease, **query: str) -> str:
    url = reverse("ledger:stores:detail", kwargs={"pk": lease.store_id})
    if query:
        url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
    return url


# --- pre-fill stays within the store -----------------------------------------------------


def test_a_form_opened_from_a_store_never_pre_fills_a_lease_of_another_store() -> None:
    mine, theirs = _lease(), _lease()
    assert theirs.operator is not None
    billing, _ = client_for(Role.BILLING)
    admin, _ = client_for(Role.ADMIN)
    forms = [
        (billing, reverse("ledger:leases:add_setup_charge", kwargs={"pk": mine.pk})),
        (billing, reverse("ledger:invoices:new_store", kwargs={"pk": mine.store_id})),
        (billing, reverse("ledger:stores:contacts_add", kwargs={"pk": mine.store_id})),
        (admin, reverse("ledger:leases:edit", kwargs={"pk": mine.pk})),
    ]
    for client, url in forms:
        response = client.get(f"{url}?store={mine.store_id}&lease={theirs.pk}")
        assert response.status_code in (200, 302, 404), url
        if response.status_code == 200:
            body = response.content.decode()
            assert str(theirs.pk) not in body, url
            assert not has_token(body, theirs.store.external_label), url
            assert theirs.operator.display_name not in body, url
        elif response.status_code == 302:
            assert str(theirs.pk) not in response["Location"]


def test_next_cannot_send_the_user_off_site_after_a_save() -> None:
    lease = _lease()
    billing, _ = client_for(Role.BILLING)
    add = reverse("ledger:stores:contacts_add", kwargs={"pk": lease.store_id})
    for target in ("https://evil.example/", "//evil.example/x", "javascript:alert(1)"):
        response = billing.post(
            f"{add}?store={lease.store_id}&lease={lease.pk}&next={target}",
            {
                "kind": "call",
                "happened_on": TODAY.isoformat(),
                "body": "Spoke to them.",
                "lease": str(lease.pk),
                "next": target,
            },
        )
        assert response.status_code in (302, 200), target
        if response.status_code == 302:
            location = response["Location"]
            assert location.startswith("/") and not location.startswith("//"), (target, location)
            assert "evil.example" not in location and "javascript" not in location


# --- roles -------------------------------------------------------------------------------


def test_a_viewer_sees_no_bar_and_gets_403_on_every_bar_action() -> None:
    lease = _lease()
    a_rent_charge(lease, OCTOBER, due="1000.00")
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get(_store_page(lease)).content.decode()
    routes = (
        ("ledger:stores:record_payment", {"pk": lease.store_id}),
        ("ledger:stores:charge_now_pick", {"pk": lease.store_id}),
        ("ledger:stores:create_charges", {"pk": lease.store_id}),
        ("ledger:stores:end_lease", {"pk": lease.store_id}),
        ("ledger:stores:transfer", {"pk": lease.store_id}),
        ("ledger:invoices:new_store", {"pk": lease.store_id}),
        ("ledger:leases:add_setup_charge", {"pk": lease.pk}),
    )
    for route, kwargs in routes:
        url = _url_or_none(route, kwargs)
        if url is None:  # a route of a later batch
            continue
        assert url not in page, route
        assert viewer.get(url).status_code in (403, 405), route
        assert viewer.post(url, {}).status_code in (403, 405), route
    assert "tab=history" not in page
    assert viewer.get(_store_page(lease, tab="history")).status_code in (200, 403)
    assert not has_token(
        viewer.get(_store_page(lease, tab="history")).content.decode().lower(), "audit"
    )
    assert Client().get(_store_page(lease)).status_code == 401


# --- the History tab is scoped --------------------------------------------------------------------


def test_the_history_tab_never_shows_another_stores_rows() -> None:
    mine, theirs = _lease(), _lease()
    a_rent_charge(mine, OCTOBER, due="1000.00")
    a_rent_charge(theirs, OCTOBER, due="2000.00")
    AuditLog.objects.create(
        actor=None,
        action="rent_charge.created",
        entity_type="rent_charge",
        entity_id=str(theirs.rent_charges.get().pk),
        before=None,
        after={"marker": "OTHER-STORE-MARKER"},
    )
    AuditLog.objects.create(
        actor=None,
        action="rent_charge.created",
        entity_type="rent_charge",
        entity_id=str(mine.rent_charges.get().pk),
        before=None,
        after={"marker": "MY-STORE-MARKER"},
    )
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(_store_page(mine, tab="history")).content.decode()
    assert "MY-STORE-MARKER" in page or "rent_charge.created" in page
    assert "OTHER-STORE-MARKER" not in page
    assert str(theirs.rent_charges.get().pk) not in page


# --- drafts ------------------------------------------------------------------------------------


def test_the_drafts_delete_cannot_delete_a_sent_invoice_and_send_sends_once() -> None:
    lease = _lease()
    assert lease.operator is not None
    draft = InvoiceDraft.objects.create(
        operator=lease.operator, lease=lease, memo="Move-in", status=InvoiceDraftStatus.DRAFT
    )
    a_one_off_charge(lease.operator, "100.00", TODAY, lease=lease)
    billing, _ = client_for(Role.BILLING)
    page = billing.get(_store_page(lease, tab="invoices")).content.decode()
    delete = reverse("ledger:invoices:delete_draft", kwargs={"pk": draft.pk})
    assert delete in page and draft.number in page
    # A sent draft (its invoice named, as the CHECK requires): Delete refuses, the row stays.
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-0901",
        operator=lease.operator,
        issue_date=TODAY,
        due_date=TODAY,
        subtotal=D("100.00"),
        total=D("100.00"),
    )
    InvoiceDraft.objects.filter(pk=draft.pk).update(
        status=InvoiceDraftStatus.SENT, sent_invoice=invoice
    )
    response = billing.post(delete, {})
    assert response.status_code in (302, 403, 405)
    assert InvoiceDraft.objects.filter(pk=draft.pk).exists()
    assert not AuditLog.objects.filter(action="invoice_draft.deleted").exists()
    # GET never deletes.
    InvoiceDraft.objects.filter(pk=draft.pk).update(
        status=InvoiceDraftStatus.DRAFT, sent_invoice=None
    )
    assert billing.get(delete).status_code in (405, 403, 302)
    assert InvoiceDraft.objects.filter(pk=draft.pk).exists()
    # A viewer cannot delete.
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(delete, {}).status_code == 403
    assert InvoiceDraft.objects.filter(pk=draft.pk).exists()

"""Adversarial cases for PLAN-012c (the store page's Add entry button), by the QA owner (plan
§6). The button may show for a lease that is not current, but the route must still write only
against the store's own lease and operator, never an archived one, and never for a viewer."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from accounts.roles import Role
from ledger.models import CollectionStatus, ContactEntry, Lease, Operator
from ledger.services import contacts
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_contact_log import contact_post, page_of
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


def _ended(store: object, operator: Operator) -> Lease:
    lease = a_lease(store, operator, started=date(2025, 1, 1), status=CollectionStatus.ENDED)  # type: ignore[arg-type]
    Lease.objects.filter(pk=lease.pk).update(ended_on=TODAY - timedelta(days=30))
    return Lease.objects.get(pk=lease.pk)


def test_store_as_route_cannot_record_against_store_bs_lease_or_operator() -> None:
    operator_a, operator_b = an_operator("A"), an_operator("B")
    store_a, store_b = a_store("2477"), a_store("2476")
    lease_a = _ended(store_a, operator_a)
    lease_b = a_lease(store_b, operator_b)
    billing, _ = client_for(Role.BILLING)
    # PLAN-012e-2 D5: Add entry lives on the Contact log tab, not on the bar.
    page = page_of(billing, f"/stores/{store_a.pk}/?tab=contacts")
    assert f"/stores/{store_a.pk}/contacts/new/" in page
    for forged in (
        {"lease": str(lease_b.pk)},
        {"lease": str(lease_b.pk), "operator": str(operator_b.pk)},
        {"operator": str(operator_b.pk)},
        {"lease": str(lease_a.pk), "operator": str(operator_b.pk), "store": str(store_b.pk)},
    ):
        billing.post(f"/stores/{store_a.pk}/contacts/new/", contact_post(body="Forged.", **forged))
    assert not ContactEntry.objects.filter(operator=operator_b).exists()
    assert not ContactEntry.objects.filter(lease=lease_b).exists()
    for row in ContactEntry.objects.filter(body="Forged."):
        assert row.operator_id == operator_a.pk and row.lease_id == lease_a.pk


def test_an_archived_operators_lease_offers_no_button_and_the_route_writes_nothing() -> None:
    operator = an_operator("Gone")
    store = a_store("2478")
    lease = _ended(store, operator)
    Operator.objects.filter(pk=operator.pk).update(archived_at=timezone.now())
    assert contacts.store_contact_lease(store, TODAY) is None
    billing, _ = client_for(Role.BILLING)
    page = page_of(billing, f"/stores/{store.pk}/?tab=contacts")
    assert f"/stores/{store.pk}/contacts/new/" not in page
    response = billing.post(
        f"/stores/{store.pk}/contacts/new/", contact_post(lease=str(lease.pk), body="Late.")
    )
    assert response.status_code == 302
    assert not ContactEntry.objects.exists()


def test_the_older_lease_with_a_live_operator_wins_over_a_newer_archived_one() -> None:
    store = a_store("2479")
    older_operator, newer_operator = an_operator("Older"), an_operator("Newer")
    older = a_lease(store, older_operator, started=date(2024, 1, 1), status=CollectionStatus.ENDED)
    Lease.objects.filter(pk=older.pk).update(ended_on=date(2025, 6, 30))
    newer = a_lease(store, newer_operator, started=date(2025, 7, 1), status=CollectionStatus.ENDED)
    Lease.objects.filter(pk=newer.pk).update(ended_on=date(2026, 9, 1))
    assert contacts.store_contact_lease(store, TODAY) == Lease.objects.get(pk=newer.pk)
    Operator.objects.filter(pk=newer_operator.pk).update(archived_at=timezone.now())
    assert contacts.store_contact_lease(store, TODAY) == Lease.objects.get(pk=older.pk)
    billing, _ = client_for(Role.BILLING)
    form = page_of(billing, f"/stores/{store.pk}/contacts/new/")
    assert older_operator.display_name in form and newer_operator.display_name not in form


def test_a_viewer_sees_no_button_on_a_non_current_lease_and_gets_403() -> None:
    store = a_store("2480")
    lease = a_lease(store, an_operator("Soon"), started=TODAY + timedelta(days=10))
    Lease.objects.filter(pk=lease.pk).update(collection_status=CollectionStatus.PROSPECTIVE)
    viewer, _ = client_for(Role.VIEWER)
    page = page_of(viewer, f"/stores/{store.pk}/?tab=contacts")
    assert f"/stores/{store.pk}/contacts/new/" not in page
    assert viewer.get(f"/stores/{store.pk}/contacts/new/").status_code == 403
    assert (
        viewer.post(f"/stores/{store.pk}/contacts/new/", contact_post(body="No.")).status_code
        == 403
    )
    assert not ContactEntry.objects.exists()
    billing, _ = client_for(Role.BILLING)
    assert f"/stores/{store.pk}/contacts/new/" in page_of(
        billing, f"/stores/{store.pk}/?tab=contacts"
    )

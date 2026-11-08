"""Fable's adversarial cases for PLAN-015b (docs/qa/15/PLAN-015b-lease-status.md §5), beside
Opus's ``test_lease_status_views.py``: an Edit lease save while collection is paused bills
nothing; a legacy paused lease with no pause row resumes and queues no autopay; a POST without
the CSRF token to Resume and to Activate is refused.

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; its call list stays empty.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import CollectionStatus, Lease, PaymentChannel, RentCharge, ScheduleState
from ledger.services import leases as service
from payments.fake import FakeProvider
from tests.auth_helpers import make_user, verified_client
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_edit_lease_page import data as edit_data
from tests.test_edit_lease_page import path as edit_path
from tests.test_lease_status_views import autopay_on
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 11, 7)


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def a_world(status: str = CollectionStatus.ACTIVE, **fields: Any) -> Lease:
    return a_lease(
        a_store("Istanbul", 1991),
        an_operator("Fatih Co"),
        started=date(2026, 9, 1),
        debit_day=1,
        status=status,
        **fields,
    )


def charges(lease: Lease) -> int:
    return RentCharge.objects.filter(lease=lease).count()


def test_an_edit_lease_save_while_paused_bills_nothing_and_the_resume_bills_the_months(
    fake: FakeProvider,
) -> None:
    """The Edit lease page's save runs the rent catch-up, which acts on an active lease only:
    a paused lease saved with a new channel gets no charge; resuming then charges the months."""
    lease = a_world(CollectionStatus.PAUSED)
    admin = client_for(Role.ADMIN, "admin-paused-edit")[0]
    response = admin.post(
        edit_path(lease), edit_data(lease, payment_channel=PaymentChannel.INVOICE)
    )
    assert response.status_code == 302
    lease.refresh_from_db()
    assert lease.payment_channel == PaymentChannel.INVOICE
    assert lease.collection_status == CollectionStatus.PAUSED
    assert charges(lease) == 0
    service.resume_collection(lease.pk, actor=None, today=TODAY)
    assert charges(lease) == 3  # September, October, November
    assert fake.calls == []


def test_a_legacy_paused_lease_with_no_pause_row_resumes_and_queues_no_autopay(
    fake: FakeProvider,
) -> None:
    lease = a_world(CollectionStatus.PAUSED)
    autopay_on(lease, ScheduleState.PAUSED)
    assert service.collection_pause(lease) is None
    outcome = service.resume_collection(lease.pk, actor=None, today=TODAY)
    assert outcome.autopay_resume is False
    assert Lease.objects.get(pk=lease.pk).collection_status == CollectionStatus.ACTIVE
    assert fake.calls == []


@pytest.mark.parametrize("action", ["resume-collection", "activate"])
def test_a_post_without_the_csrf_token_is_refused_and_changes_nothing(action: str) -> None:
    status = (
        CollectionStatus.PAUSED if action == "resume-collection" else CollectionStatus.PROSPECTIVE
    )
    # The role each route needs (§9.3: Resume is admin, Activate billing), so only the
    # missing token refuses the POST.
    role = Role.ADMIN if action == "resume-collection" else Role.BILLING
    lease = a_world(status)
    user, device = make_user(f"csrf-{action}", role)
    assert device is not None
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    response = client.post(f"/leases/{lease.pk}/{action}/", {})
    assert response.status_code == 403
    assert Lease.objects.get(pk=lease.pk).collection_status == status

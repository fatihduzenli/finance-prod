"""Fable's adversarial cases for the Edit lease page (docs/qa/15/PLAN-015a-edit-lease.md §5),
beside Opus's ``test_edit_lease_page.py``: a rent change on a lease whose next rent is already
booked, a POST without the CSRF token, a billing company name carrying markup, and a posted day
that is not a choice while the current day is above the 28th.

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; nothing leaves.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import Lease, LeaseBilling, LeaseRentHistory, RentSource
from ledger.services.leases import RENT_DATE_TOO_EARLY
from payments.fake import FakeProvider
from tests.auth_helpers import make_user, verified_client
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_edit_lease_page import data, page, path
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 1)
START = date(2026, 1, 13)


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def admin() -> Client:
    return client_for(Role.ADMIN, "admin-edit-lease-adversarial")[0]


def a_lease_with_a_booked_rent() -> Lease:
    """$3,500.00 from the start, $3,600.00 booked from Dec 1, 2026 (the open row)."""
    lease = a_lease(a_store("Istanbul", 1991), an_operator("Fatih Co"), started=START)
    LeaseRentHistory.objects.filter(lease=lease, effective_to__isnull=True).update(
        effective_to=date(2026, 11, 30)
    )
    LeaseRentHistory.objects.create(
        lease=lease, amount=D("3600.00"), effective_from=date(2026, 12, 1), source=RentSource.STAFF
    )
    return lease


def history(lease: Lease) -> list[tuple[Decimal, date, date | None]]:
    return [
        (row.amount, row.effective_from, row.effective_to)
        for row in LeaseRentHistory.objects.filter(lease=lease).order_by("effective_from")
    ]


def test_a_new_rent_with_the_default_date_is_refused_when_the_next_rent_is_already_booked(
    admin: Client,
) -> None:
    """The default date is the next rent due day (Oct 5, 2026), before the booked row's start:
    the service refuses on the date field and the history keeps its two rows."""
    lease = a_lease_with_a_booked_rent()
    before = history(lease)
    response = admin.post(path(lease), data(lease, rent="3700.00"))
    assert response.status_code == 302
    body = page(admin, lease)
    assert RENT_DATE_TOO_EARLY.format(start="Dec 1, 2026") in body
    assert history(lease) == before


def test_a_new_rent_dated_after_the_booked_row_replaces_it(admin: Client) -> None:
    """Dated two days after the booked row's start, the change closes that row the day before
    and opens the new one; the first row is untouched."""
    lease = a_lease_with_a_booked_rent()
    response = admin.post(
        path(lease), data(lease, rent="3700.00", rent_effective_from="2026-12-03")
    )
    assert response.status_code == 302
    assert history(lease) == [
        (D("3500.00"), START, date(2026, 11, 30)),
        (D("3600.00"), date(2026, 12, 1), date(2026, 12, 2)),
        (D("3700.00"), date(2026, 12, 3), None),
    ]


def test_a_post_without_the_csrf_token_is_refused_and_changes_nothing() -> None:
    user, device = make_user("admin-edit-lease-csrf", Role.ADMIN)
    assert device is not None
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    lease = a_lease(a_store("Istanbul", 1991), an_operator("Fatih Co"), started=START)
    response = client.post(path(lease), data(lease, debit_day="9"))
    assert response.status_code == 403
    assert Lease.objects.get(pk=lease.pk).debit_day == 5


def test_a_billing_company_name_with_markup_is_escaped_in_the_summary(admin: Client) -> None:
    lease = a_lease(a_store("Istanbul", 1991), an_operator("Fatih Co"), started=START)
    LeaseBilling.objects.create(
        lease=lease, company_name='<img src=x onerror="alert(1)">Evil LLC', email="e@example.com"
    )
    body = page(admin, lease)
    assert "&lt;img src=x onerror=" in body
    assert '<img src=x onerror="alert(1)">' not in body


def test_a_day_above_the_28th_other_than_the_current_one_is_refused(admin: Client) -> None:
    """A current 31st is one more choice; the 30th is not, even on that lease."""
    lease = a_lease(a_store("Istanbul", 1991), an_operator("Fatih Co"), started=START, debit_day=31)
    response = admin.post(path(lease), data(lease, debit_day="30"))
    assert response.status_code == 302
    body = page(admin, lease)
    assert "Choose one of the options." in body
    assert Lease.objects.get(pk=lease.pk).debit_day == 31

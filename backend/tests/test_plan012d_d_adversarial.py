"""Adversarial cases for PLAN-012d Batch D (operations pages), by the QA owner (plan §6 D).
"Create missing charges" on one store never touches another store and is refused below
billing; the grid's month picker swaps a reversed range and caps it at 24 months; the run
page's list of created charges names only what it created."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import Lease, PaymentChannel, RentCharge
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


def _lease(started: date) -> Lease:
    return a_lease(a_store(), an_operator(), started=started, channel=PaymentChannel.WIRE)


def _months(lease: Lease) -> list[date]:
    return list(
        RentCharge.objects.filter(lease=lease)
        .order_by("period_month")
        .values_list("period_month", flat=True)
    )


# --- Create missing charges stays on its store -----------------------------------------------


def test_create_missing_charges_stays_on_its_store_and_is_refused_below_billing() -> None:
    mine, theirs = _lease(date(2026, 8, 13)), _lease(date(2026, 8, 13))
    url = reverse("ledger:stores:create_charges", kwargs={"pk": mine.store_id})
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(url, {}).status_code == 403
    assert Client().post(url, {}).status_code == 401
    assert _months(mine) == [] and _months(theirs) == []
    billing, _ = client_for(Role.BILLING)
    response = billing.post(url, {})
    assert response.status_code == 302 and "tab=charges" in response["Location"]
    assert _months(mine) == [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    assert _months(theirs) == []
    # A second click creates nothing and says so.
    again = billing.post(url, {}, follow=True)
    assert again.status_code == 200
    assert "Nothing missing" in again.content.decode()
    assert len(_months(mine)) == 3
    # GET is not an action.
    assert billing.get(url).status_code in (405, 302)
    assert len(_months(mine)) == 3


# --- the grid's picker ----------------------------------------------------------------------------


def _grid_months(body: str) -> str:
    """The month headers of the grid table only (the pickers list many months)."""
    head = body.split("<thead", 1)[1].split("</thead>", 1)[0]
    return head


def test_the_grid_picker_swaps_a_reversed_range_and_caps_it_at_twenty_four_months() -> None:
    _lease(date(2026, 1, 1))
    viewer, _ = client_for(Role.VIEWER)
    grid = reverse("ledger:grid:page")
    default = viewer.get(grid)
    assert default.status_code == 200
    body = default.content.decode()
    months = _grid_months(body)
    # 10 past months, the current and the next: December 2025 to November 2026.
    assert "Dec 2025" in months and "Nov 2026" in months  # PLAN-012f-a B4: "Oct 2026"
    assert "Nov 2025" not in months and "Dec 2026" not in months
    reversed_range = viewer.get(f"{grid}?from=2026-11&to=2026-06")
    assert reversed_range.status_code == 200
    swapped = _grid_months(reversed_range.content.decode())
    assert "Jun 2026" in swapped and "Nov 2026" in swapped
    too_wide = viewer.get(f"{grid}?from=2020-01&to=2026-12")
    assert too_wide.status_code == 200
    wide = _grid_months(too_wide.content.decode())
    assert "Jan 2020" not in wide and "Dec 2026" in wide and "Jan 2025" in wide
    hostile = viewer.get(f"{grid}?from=2026-13&to=%3Cscript%3Ealert(1)%3C/script%3E")
    assert hostile.status_code == 200
    hostile_body = hostile.content.decode()
    assert "alert(1)" not in hostile_body and "2026-13" not in hostile_body
    assert hostile_body.count("<script") == body.count("<script")
    assert _grid_months(hostile_body) == months  # the default window again


# --- the run page's list --------------------------------------------------------------------------


def test_the_run_page_lists_only_what_it_just_created_with_store_links() -> None:
    created_for, untouched = _lease(date(2026, 9, 13)), _lease(date(2026, 9, 1))
    RentCharge.objects.create(
        lease=untouched,
        period_month=date(2026, 9, 1),
        seq=1,
        due_on=date(2026, 9, 1),
        amount_due=D("3500.00"),
        status="due",
    )
    RentCharge.objects.create(
        lease=untouched,
        period_month=date(2026, 10, 1),
        seq=1,
        due_on=date(2026, 10, 1),
        amount_due=D("3500.00"),
        status="due",
    )
    billing, _ = client_for(Role.BILLING)
    response = billing.post(reverse("ledger:run:create_charges"), {}, follow=True)
    assert response.status_code == 200
    body = response.content.decode()
    store_link = reverse("ledger:stores:detail", kwargs={"pk": created_for.store_id})
    other_link = reverse("ledger:stores:detail", kwargs={"pk": untouched.store_id})
    assert 'id="created-charges"' in body
    created_table = body.split('id="created-charges"')[1].split("</table>")[0]
    assert store_link in created_table and "catch-up" in created_table
    assert other_link not in created_table
    assert _months(untouched) == [date(2026, 9, 1), date(2026, 10, 1)]

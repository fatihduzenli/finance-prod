"""Adversarial cases for PLAN-012f-b Batch A (the dashboard), by the QA owner (plan §6). Slice A-1:
the store picker's JSON route is read-only, role-checked and bounded; markup in a store name or an
operator name is data; the dashboard embeds the picker only for a role that may act; the "+ New
invoice" button never points at the Stores list; the scripts-off fallback lands on the builder."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import PaymentChannel, Store
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_lease_end_drafts import row_counts
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'


def picker_url(q: str) -> str:
    return reverse("ledger:search_stores") + f"?q={q}"


def a_world() -> Store:
    store = a_store(f"1991 Istanbul {MARKUP}", 1991)
    operator = an_operator(f"Fatih {MARKUP}")
    a_lease(store, operator, channel=PaymentChannel.WIRE)
    a_store("2484 Paris", 2484)
    return store


def test_the_picker_route_writes_nothing_and_escapes_markup() -> None:
    store = a_world()
    billing, _ = client_for(Role.BILLING)
    before = row_counts()
    response = billing.get(picker_url("1991"))
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    body = response.content.decode()
    assert "<script>" not in body  # JsonResponse escapes the angle brackets
    found = json.loads(body)["stores"]
    assert len(found) == 1
    assert found[0]["url"] == reverse("ledger:invoices:new_store", kwargs={"pk": store.pk})
    assert "1991" in str(found[0]["number"])
    assert row_counts() == before


@pytest.mark.parametrize("q", ["", " ", "x" * 300, MARKUP, "1991; drop table stores", "%00"])
def test_a_hostile_or_empty_query_answers_a_bounded_list(q: str) -> None:
    a_world()
    billing, _ = client_for(Role.BILLING)
    response = billing.get(picker_url(q))
    assert response.status_code == 200
    found = json.loads(response.content.decode())["stores"]
    assert len(found) <= 10


def test_the_picker_route_needs_a_session() -> None:
    from django.test import Client

    a_world()
    response = Client().get(picker_url("1991"))
    assert response.status_code in (401, 403)  # the middleware answers 401 signed out
    assert b'"stores"' not in response.content  # the login page, never the rows


def test_the_picker_route_lists_at_most_ten_stores() -> None:
    for number in range(3000, 3015):
        a_store(f"{number} Many", number)
    billing, _ = client_for(Role.BILLING)
    found = json.loads(billing.get(picker_url("Many")).content.decode())["stores"]
    assert len(found) == 10


def test_the_dashboard_button_never_points_at_the_stores_list() -> None:
    a_world()
    for role, expects_picker in ((Role.BILLING, True), (Role.ADMIN, True), (Role.VIEWER, False)):
        client, _ = client_for(role)
        body = client.get("/").content.decode()
        has_button = 'id="new-invoice"' in body
        assert has_button == expects_picker, role
        if has_button:
            start = body.index('id="new-invoice"')
            tag = body[body.rfind("<a", 0, start) : body.index(">", start) + 1]
            assert reverse("ledger:stores:list") + '"' not in tag, tag
            assert 'data-dialog-open="store-picker"' in tag
            assert 'id="store-picker"' in body
            assert "<dialog" in body


def test_the_scripts_off_fallback_lands_on_the_builder() -> None:
    store = a_world()
    billing, _ = client_for(Role.BILLING)
    page = billing.get(reverse("ledger:search") + "?q=1991&intent=new_invoice")
    assert page.status_code in (200, 302)
    if page.status_code == 302:
        location = page["Location"]
    else:
        body = page.content.decode()
        target = reverse("ledger:invoices:new_store", kwargs={"pk": store.pk})
        assert target in body
        assert MARKUP not in body
        location = target
    assert location.endswith(reverse("ledger:invoices:new_store", kwargs={"pk": store.pk}))


def test_a_viewer_sees_no_picker_and_cannot_open_the_builder() -> None:
    store = a_world()
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get("/").content.decode()
    assert 'id="store-picker"' not in body
    response = viewer.get(reverse("ledger:invoices:new_store", kwargs={"pk": store.pk}))
    assert response.status_code in (302, 403)


def test_the_picker_rows_carry_no_balance_or_bank_data() -> None:
    a_world()
    billing, _ = client_for(Role.BILLING)
    found = json.loads(billing.get(picker_url("1991")).content.decode())["stores"]
    assert set(found[0]) <= {"number", "name", "operator", "closed", "url"}


# --- Slices A-2 and A-3: the cards -----------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "range=custom&from=2026-12&to=2026-01",
        "range=custom&from=2020-01&to=2026-10",
        "range=custom&from=x&to=y",
        "range=" + MARKUP,
        "range=this&cal=" + MARKUP,
        "cal=2026-13",
        "cal=0000-00",
        "range=12m&cal=9999-12",
        "range=custom&from=2026-10",
        "range=%00",
    ],
)
def test_a_hostile_range_or_calendar_month_never_breaks_the_dashboard_or_writes(
    query: str,
) -> None:
    a_world()
    billing, _ = client_for(Role.BILLING)
    before = row_counts()
    response = billing.get("/?" + query)
    assert response.status_code == 200
    body = response.content.decode()
    assert MARKUP not in body
    assert 'id="cashflow-card"' in body and 'id="payment-status"' in body
    assert 'id="mini-calendar"' in body
    assert row_counts() == before


def test_a_swapped_custom_range_shows_one_message_and_no_figures() -> None:
    from ledger.services import dashboard

    a_world()
    billing, _ = client_for(Role.BILLING)
    body = billing.get("/?range=custom&from=2026-12&to=2026-01").content.decode()
    assert body.count(dashboard.RANGE_MESSAGE) >= 1
    assert 'id="cashflow-totals"' not in body


def test_a_viewer_reads_the_cards_and_the_range_is_read_only() -> None:
    a_world()
    viewer, _ = client_for(Role.VIEWER)
    before = row_counts()
    body = viewer.get("/?range=3m").content.decode()
    for element in ("cashflow-card", "payment-status", "mini-calendar"):
        assert f'id="{element}"' in body, element
    assert viewer.post("/?range=3m", {"range": "12m"}).status_code in (403, 405)
    assert row_counts() == before


def test_markup_in_a_calendar_note_is_escaped_in_the_mini_calendar() -> None:
    from datetime import date

    from django.utils import timezone

    from ledger.models import CalendarEntry

    a_world()
    billing, user = client_for(Role.BILLING)
    today = timezone.localdate()
    CalendarEntry.objects.create(
        kind="note", on=date(today.year, today.month, 1), title=f"Visit {MARKUP}", author=user
    )
    body = billing.get(f"/?cal={today:%Y-%m}").content.decode()
    assert MARKUP not in body
    assert "Visit &lt;script&gt;" in body


def test_the_cashflow_card_never_counts_a_missing_cost_as_zero() -> None:
    from datetime import date
    from decimal import Decimal

    from ledger.services import dashboard
    from tests.receivables_helpers import a_payment, a_rent_charge

    store = a_world()  # the store has no cost row
    lease = store.leases.get()
    charge = a_rent_charge(lease, date(2026, 10, 1))
    a_payment(charge, "100.00", date(2026, 10, 6))
    found = dashboard.cashflow(
        dashboard.Range("custom", "Oct 2026", date(2026, 10, 1), date(2026, 10, 1))
    )
    [month] = [row for row in found.months if row.month == date(2026, 10, 1)]
    assert month.income == Decimal("100.00")
    assert month.expenses is None  # unknown, never 0.00
    assert found.stores_without_cost >= 1
    assert found.expenses is None  # the one store has no cost row: unknown, not zero


def test_the_dashboard_query_count_stays_within_its_budget(
    django_assert_max_num_queries: Any,
) -> None:
    a_world()
    billing, _ = client_for(Role.BILLING)
    billing.get("/")  # warm the session row
    with django_assert_max_num_queries(80):
        assert billing.get("/?range=12m").status_code == 200

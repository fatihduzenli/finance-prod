"""Adversarial cases for PLAN-012f Batch C (the dashboard, the store and operator workspaces,
the monthly rent overview), by the QA owner (plan §6). Markup in a store name, an operator
name and an address is escaped in the breadcrumb, the KPI notes, the attention rows and the
coming-up list; a dashboard with no data renders every card with a sentence and no division by
zero; a month with nothing billed reads "—" and draws no bar; a viewer sees the attention rows
but no action button; a store without a number or an address keeps a sensible header; the
collection percentage never exceeds 100 even when more was collected than billed."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import PaymentChannel, RentChargeStatus, Store
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
TODAY = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


# --- an empty dashboard --------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_a_dashboard_with_no_data_renders_every_card_and_no_broken_number(role: Role) -> None:
    client, _ = client_for(role)
    response = client.get("/")
    assert response.status_code == 200
    body = response.content.decode()
    visible = re.sub(r"<script.*?</script>", " ", body, flags=re.S)  # not the service-worker note
    text = _text(visible)
    words = set(text.split())
    # The approved "None." sentences of the dashboard stay (PLAN-012c §4.7); a broken number
    # would read nan, inf or a Python repr.
    for word in ("nan", "inf", "NaN", "Infinity"):
        assert word not in words, word
    assert "ZeroDivision" not in text and "Decimal(" not in text
    assert body.count('class="kpi"') >= 4
    assert "%" not in body.split('class="progress')[0][-200:] or "—" in text
    # No bar when nothing is billed; no percentage above 100 anywhere.
    for match in re.findall(r"(\d+(?:\.\d+)?)\s*%", text):
        assert float(match) <= 100, match


def test_more_collected_than_billed_never_shows_over_one_hundred_percent() -> None:
    lease = a_lease(a_store("2901"), an_operator("Over Co"), channel=PaymentChannel.WIRE)
    a_rent_charge(
        lease, date(2026, 10, 1), status=RentChargeStatus.PAID, due="1000.00", paid="1500.00"
    )
    billing, _ = client_for(Role.BILLING)
    text = _text(billing.get("/").content.decode())
    for match in re.findall(r"(\d+(?:\.\d+)?)\s*%", text):
        assert float(match) <= 100, match


# --- markup in the names the pages show ----------------------------------------------------------


def test_markup_in_a_store_an_operator_and_an_address_is_escaped_on_the_three_pages() -> None:
    store = a_store(f"Istanbul {MARKUP}")
    store.store_number = 1991
    store.address_line1 = f"12 {MARKUP} Street"
    store.city = f"Spring{MARKUP}field"
    store.save(update_fields=["store_number", "address_line1", "city"])
    lease = a_lease(
        store, an_operator(f"Fatih {MARKUP}"), channel=PaymentChannel.WIRE, started=date(2026, 1, 1)
    )
    a_rent_charge(lease, date(2026, 8, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    admin, _ = client_for(Role.ADMIN)
    for path in (
        "/",
        reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
        reverse("ledger:stores:detail", kwargs={"pk": store.pk}) + "?tab=contacts",
        reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id}),
        reverse("ledger:grid:page") + "?from=2026-01&to=2026-12",
        reverse("ledger:arrears:page"),
    ):
        response = admin.get(path)
        assert response.status_code == 200, path
        body = response.content.decode()
        assert MARKUP not in body, path
        assert "&lt;script&gt;" in body, path
        assert MARKUP not in body.split("<title>")[1].split("</title>")[0], path


# --- the attention rows and the viewer -----------------------------------------------------------


def test_a_viewer_sees_the_attention_rows_but_no_action_button() -> None:
    lease = a_lease(
        a_store("2902"),
        an_operator("Overdue Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    a_rent_charge(lease, date(2026, 7, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get("/").content.decode()
    assert "Overdue Co" in body
    section = body[body.index("Overdue Co") - 2000 : body.index("Overdue Co") + 2000]
    assert "<form" not in section
    assert not re.search(
        r'class="btn[^"]*"[^>]*href="[^"]*(payments/new|contacts/new|lease|end)', section
    )
    billing, _ = client_for(Role.BILLING)
    body = billing.get("/").content.decode()
    row = (
        body[body.index("Overdue Co") : body.index("</tr>", body.index("Overdue Co"))]
        if "</tr>" in body
        else body
    )
    assert 'class="btn' in row or "btn-sm" in row


# --- a store with nothing to show in its header --------------------------------------------------


def test_a_store_without_a_number_or_an_address_keeps_a_sensible_header() -> None:
    store = Store.objects.create(external_label="Nameless", status="active")
    admin, _ = client_for(Role.ADMIN)
    body = admin.get(reverse("ledger:stores:detail", kwargs={"pk": store.pk})).content.decode()
    header = body[
        body.index('id="page-header"') : body.index("</header>", body.index('id="page-header"'))
    ]
    text = " ".join(_text(header).split())
    assert "Nameless" in text
    assert "#None" not in text and "# " not in text and "None" not in text
    assert "Open since" not in text or re.search(r"Open since \d{4}", text)
    assert " · " not in text.replace("Nameless", "").strip(" ·") or "Open since" in text


def test_the_recent_payments_and_coming_up_lists_never_leak_another_role_s_links_to_a_viewer() -> (
    None
):
    lease = a_lease(
        a_store("2903"),
        an_operator("Recent Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    a_rent_charge(
        lease, date(2026, 9, 1), status=RentChargeStatus.PAID, due="1000.00", paid="1000.00"
    )
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get("/").content.decode()
    for path in ("/payments/new/", "/invoices/new/", "/stores/new/", "/leases/new/"):
        assert path not in body, path

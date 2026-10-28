"""Adversarial cases for PLAN-012f-a Batch B (the list family, D3; the detail family, D4), by
the QA owner (plan §6). Every filter control a list page renders is fed markup, an out-of-range
value and an overlong value: the page answers 200 (or redirects on site), echoes the hostile
value nowhere outside an attribute value, and falls back to its defaults; a viewer sees no
primary action in any list's header and no link to a form; the stores list's counts line and
"filtered from" footer equal the database; the balance badge never names a wrong family; the
detail pages' secondary actions follow the role; markup in a store's and an operator's name is
escaped on every list and detail page."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import PaymentChannel, RentChargeStatus, Store
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
HOSTILE = (MARKUP, "9" * 80, "../../etc/passwd", "balance;drop", "-1", "1e309", "\u0000", "%00")
LISTS = ("/stores/", "/operators/", "/leases/", "/payments/", "/returns/", "/arrears/")


def _controls(html: str) -> list[str]:
    """The names of the GET controls of the page's filter form(s)."""
    names: list[str] = []
    for form in re.findall(r"<form\b[^>]*method=\"get\"[^>]*>.*?</form>", html, re.S | re.I):
        names += re.findall(r'<(?:input|select)\b[^>]*\bname="([^"]+)"', form)
    return sorted(set(names))


def _outside_attributes(html: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r'="[^"]*"', '=""', text)
    return text


def _world() -> None:
    owing = a_lease(
        a_store(f"Istanbul {MARKUP}"),
        an_operator(f"Fatih {MARKUP}"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    owing.store.store_number = 1991
    owing.store.save(update_fields=["store_number"])
    a_rent_charge(owing, date(2026, 9, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    paid_up = a_lease(
        a_store("Paid Up"),
        an_operator("Paid Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    a_rent_charge(
        paid_up, date(2026, 9, 1), status=RentChargeStatus.PAID, due="2000.00", paid="2000.00"
    )
    Store.objects.create(external_label="Vacant One", status="active")


def test_every_list_filter_swallows_hostile_values_and_echoes_none_of_them() -> None:
    _world()
    viewer, _ = client_for(Role.VIEWER)
    for path in LISTS:
        page = viewer.get(path)
        assert page.status_code == 200, path
        controls = _controls(page.content.decode())
        for name in controls:
            for value in HOSTILE:
                response = viewer.get(path, {name: value})
                assert response.status_code in (200, 302), (path, name, value, response.status_code)
                if response.status_code == 302:
                    assert response["Location"].startswith("/"), (path, name)
                    continue
                body = response.content.decode()
                assert MARKUP not in body, (path, name)
                # A search box echoes its (escaped) text in "Filtered by …"; a filter never
                # echoes an unknown value: it falls back to its default.
                if name != "q" and value not in ("-1", "\u0000", "%00"):
                    assert not has_token(_outside_attributes(body), value), (path, name, value)


def test_a_viewer_sees_no_primary_action_or_form_link_on_any_list() -> None:
    _world()
    viewer, _ = client_for(Role.VIEWER)
    for path in LISTS:
        body = viewer.get(path).content.decode()
        header = body[
            body.index('id="page-header"') : body.index("</header>", body.index('id="page-header"'))
        ]
        assert "btn-primary" not in header, path
        for form_path in ("/stores/new/", "/operators/new/", "/leases/new/", "/payments/new/"):
            assert form_path not in body, (path, form_path)
    assert Client().get("/stores/").status_code in (302, 401)


def test_the_stores_counts_and_footer_match_the_database() -> None:
    _world()
    billing, _ = client_for(Role.BILLING)
    body = billing.get("/stores/").content.decode()
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S | re.I)
    text = " ".join(re.sub(r"<[^>]+>", " ", visible).split())
    total = Store.objects.count()
    with_lease = (
        Store.objects.filter(leases__isnull=False, leases__ended_on__isnull=True).distinct().count()
    )
    assert f"{total} store" in text, text[:400]
    assert f"{with_lease} with a lease" in text
    assert f"{total - with_lease} vacant" in text
    assert re.search(rf"1–{min(total, 50)} of {total}", text), text[:600]


def test_the_balance_badge_names_the_right_family_for_each_store() -> None:
    _world()
    billing, _ = client_for(Role.BILLING)
    body = billing.get("/stores/", {"q": ""}).content.decode()
    rows = re.findall(r"<tr[^>]*>.*?</tr>", body.split("<tbody", 1)[1], re.S)
    by_name = {}
    for row in rows:
        for key in ("Istanbul", "Paid Up", "Vacant One"):
            if key in row:
                by_name[key] = row
    assert set(by_name) == {"Istanbul", "Paid Up", "Vacant One"}, list(by_name)
    assert "badge-unpaid" in by_name["Istanbul"] or "badge-overdue" in by_name["Istanbul"]
    assert "$2,000.00" in by_name["Istanbul"]
    assert "badge-paid" in by_name["Paid Up"] and "Paid up" in by_name["Paid Up"]
    assert "badge-neutral" in by_name["Vacant One"] and "Vacant" in by_name["Vacant One"]
    assert "badge-unpaid" not in by_name["Paid Up"] and "badge-paid" not in by_name["Vacant One"]


def test_markup_in_names_is_escaped_on_every_list_and_on_the_detail_pages() -> None:
    _world()
    lease = a_lease(
        a_store(f"Detail {MARKUP}"),
        an_operator(f"Det {MARKUP}"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    charge = a_rent_charge(lease, date(2026, 9, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    admin, _ = client_for(Role.ADMIN)
    pages = list(LISTS) + [
        reverse("ledger:charges:detail", kwargs={"pk": charge.pk}),
        reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
        reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id}),
    ]
    for path in pages:
        response = admin.get(path)
        assert response.status_code == 200, path
        body = response.content.decode()
        assert MARKUP not in body, path
        if "Istanbul" in body or "Detail" in body or "Fatih" in body or "Det " in body:
            assert "&lt;script&gt;" in body, path  # the name is shown, escaped


def test_the_detail_pages_secondary_actions_follow_the_role() -> None:
    lease = a_lease(
        a_store("Roles"),
        an_operator("Role Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    charge = a_rent_charge(lease, date(2026, 9, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    path = reverse("ledger:charges:detail", kwargs={"pk": charge.pk})
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get(path).content.decode()
    assert "btn-primary" not in body.split('id="page-header"')[1].split("</header>")[0]
    for action in (
        "/payments/new",
        "record_payment",
        "/waive/",
        "/void/",
        "/cancel/",
        "/edit/",
        "/split/",
    ):
        assert action not in body, action
    billing, _ = client_for(Role.BILLING)
    body = billing.get(path).content.decode()
    assert "/void/" not in body  # admin only
    assert "Add payment" in body


def test_the_balance_sort_and_filter_order_and_keep_only_what_they_name() -> None:
    _world()
    big = a_lease(
        a_store("Big Owing"),
        an_operator("Big Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    a_rent_charge(big, date(2026, 8, 1), status=RentChargeStatus.UNPAID, due="9000.00")
    billing, _ = client_for(Role.BILLING)
    body = billing.get("/stores/", {"balance": "owing", "sort": "balance"}).content.decode()
    rows = re.findall(r"<tr[^>]*>.*?</tr>", body.split("<tbody", 1)[1], re.S)
    names = [re.sub(r"<[^>]+>", " ", row).split()[0:4] for row in rows]
    assert any("Big" in " ".join(n) for n in names) and any(
        "Istanbul" in " ".join(n) for n in names
    )
    assert not any("Paid" in " ".join(n) or "Vacant" in " ".join(n) for n in names), names
    big_at = next(i for i, n in enumerate(names) if "Big" in " ".join(n))
    ist_at = next(i for i, n in enumerate(names) if "Istanbul" in " ".join(n))
    assert big_at < ist_at  # largest owed first
    # An unknown sort or balance falls back to the defaults (every store, by number).
    fallback = billing.get("/stores/", {"balance": "x", "sort": "y"}).content.decode()
    assert "Paid Up" in fallback and "Vacant One" in fallback


def test_the_vacant_filter_leaves_closed_stores_out_while_the_counts_line_counts_them() -> None:
    _world()
    closed = Store.objects.create(external_label="Closed Shop", status="closed")
    billing, _ = client_for(Role.BILLING)
    vacant = billing.get("/stores/", {"status": "vacant"}).content.decode()
    assert "Vacant One" in vacant and "Closed Shop" not in vacant
    closed_only = billing.get("/stores/", {"status": "closed"}).content.decode()
    assert "Closed Shop" in closed_only and "Vacant One" not in closed_only
    text = " ".join(re.sub(r"<[^>]+>", " ", billing.get("/stores/").content.decode()).split())
    # *For the owner* (PLAN-012f-a D3): "N vacant" counts every store without a lease, closed
    # ones included; the Vacant filter shows only the open ones.
    assert f"{Store.objects.count()} stores" in text
    assert "2 vacant" in text, text[text.index("stores") - 20 : text.index("stores") + 60]
    assert closed.pk

"""Adversarial cases for PLAN-012d Batch F (the Stores list and Grid filter, the top-bar
search, no repricing from the builder), by the QA owner (plan §6 F). Hostile filter text
answers 200, escaped, with no hit and no extra script; a very long query is cut; the search
never jumps to an archived operator; the filter survives the grid's form and the export; an
existing-charge line posted below its balance is refused and the draft unchanged."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    InvoiceDraft,
    InvoiceDraftLine,
    Lease,
    OneOffCharge,
    Operator,
    PaymentChannel,
)
from ledger.services import invoice_builder
from ledger.services.invoice_builder import LineInput
from payments.fake import FakeProvider
from tests import test_invoice_builder as builder_tests
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_invoice_builder import TODAY as BUILDER_TODAY
from tests.test_invoice_builder import a_world, user
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 16)
HOSTILE = [
    "' OR 1=1 --",
    "<script>alert(1)</script>",
    "%' ; DROP TABLE stores; --",
    "\\x00null",
    "🙂 émoji",
]
# The builder file's fixtures, re-exported so pytest finds them here.
fixed_now = builder_tests.fixed_now
fake = builder_tests.fake
worker = builder_tests.worker


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


def _two_stores() -> tuple[Lease, Lease]:
    first = a_lease(
        a_store("2477"),
        an_operator("Acme Stores"),
        started=date(2026, 9, 1),
        channel=PaymentChannel.WIRE,
    )
    second = a_lease(
        a_store("2478"),
        an_operator("Beacon Ltd"),
        started=date(2026, 9, 1),
        channel=PaymentChannel.WIRE,
    )
    return first, second


# --- hostile filter text ----------------------------------------------------------------


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_filter_text_answers_200_escaped_with_no_hit(hostile: str) -> None:
    _two_stores()
    viewer, _ = client_for(Role.VIEWER)
    for route in ("ledger:stores:list", "ledger:grid:page", "ledger:search"):
        plain = viewer.get(reverse(route)).content.decode()
        response = viewer.get(reverse(route), {"q": hostile})
        assert response.status_code == 200, (route, hostile)
        body = response.content.decode()
        # Markup in the query never reaches the page raw; the page's own scripts are unchanged.
        assert "<script>alert(1)</script>" not in body, route
        assert body.count("<script") == plain.count("<script"), route
        if "<" in hostile or "'" in hostile:
            assert hostile not in body, route
        # A hostile query matches nothing.
        assert "No match" in body or "No store" in body or "Filtered by" in body, route


def test_a_very_long_query_is_cut_and_answers_200() -> None:
    _two_stores()
    viewer, _ = client_for(Role.VIEWER)
    long = "a" * 5000
    for route in ("ledger:stores:list", "ledger:grid:page", "ledger:search"):
        response = viewer.get(reverse(route), {"q": long})
        assert response.status_code == 200, route
        assert long not in response.content.decode(), route


# --- the search never jumps to an archived operator --------------------------------------


def test_the_search_never_jumps_to_an_archived_operator() -> None:
    first, _ = _two_stores()
    archived = Operator.objects.create(display_name="Zebra Holdings", archived_at=timezone.now())
    assert archived.archived_at is not None
    viewer, _ = client_for(Role.VIEWER)
    response = viewer.get(reverse("ledger:search"), {"q": "Zebra"})
    assert response.status_code == 200  # a results page, not a redirect
    body = response.content.decode()
    assert "Zebra Holdings" in body and has_token(body.lower(), "archived")
    # A single live store match still jumps.
    jump = viewer.get(reverse("ledger:search"), {"q": "2477"})
    assert jump.status_code == 302
    assert jump["Location"].endswith(reverse("ledger:stores:detail", kwargs={"pk": first.store_id}))
    # Anonymous never sees results.
    assert Client().get(reverse("ledger:search"), {"q": "Zebra"}).status_code == 401


# --- the filter survives the grid's form and the export -------------------------------------


def test_the_filter_is_kept_through_the_grid_form_and_the_exports() -> None:
    first, second = _two_stores()
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get(reverse("ledger:grid:page"), {"q": "Acme"}).content.decode()
    assert has_token(page.split("<thead")[1] if "<thead" in page else page, "2477")
    assert 'name="q"' in page and 'value="Acme"' in page
    assert "q=Acme" in page  # the export link keeps it
    export = viewer.get(reverse("ledger:grid:export"), {"q": "Acme"})
    assert export.status_code == 200
    rows = b"".join(
        export.streaming_content if hasattr(export, "streaming_content") else [export.content]
    ).decode()
    assert "2477" in rows and "2478" not in rows
    stores = viewer.get(reverse("ledger:stores:export"), {"q": "Beacon"})
    assert stores.status_code == 200
    rows = b"".join(
        stores.streaming_content if hasattr(stores, "streaming_content") else [stores.content]
    ).decode()
    assert "2478" in rows and "2477" not in rows


# --- no repricing from the builder -------------------------------------------------------------


def test_an_existing_charge_line_posted_below_its_balance_is_refused_and_the_draft_unchanged(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    charge = OneOffCharge.objects.create(
        operator=operator,
        lease=lease,
        category="damage",
        collection="manual",
        description="Broken shelf",
        price=D("500.00"),
        due_date=BUILDER_TODAY,
        status="owed",
    )
    line = LineInput("Existing open charge", "", None, None, existing_charge=charge.pk)
    made = invoice_builder.create_draft(
        operator, lease, "Move-in", None, [line], user(Role.BILLING), today=BUILDER_TODAY
    )
    assert made.ok and made.draft is not None, made.message
    lower = LineInput("Existing open charge", "", None, D("450.00"), existing_charge=charge.pk)
    refused = invoice_builder.update_draft(
        made.draft.pk, lease, "Move-in", None, [lower], user(Role.BILLING), today=BUILDER_TODAY
    )
    assert not refused.ok and "edit the charge first" in refused.message
    [stored] = InvoiceDraftLine.objects.filter(draft=made.draft)
    assert stored.unit_amount == D("500.00")
    charge.refresh_from_db()
    assert charge.price == D("500.00")
    assert InvoiceDraft.objects.count() == 1

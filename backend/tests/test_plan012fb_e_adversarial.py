"""Adversarial cases for PLAN-012f-b Batch E (the pages to the spec), by the QA owner (plan §6).
Slice E-4: every confirmation page now rendered as a dialog over its parent is a read (GET writes
nothing) and refuses a viewer; the 404 and 403 pages escape what the URL carried and never leak a
path or a stack; the empty-state partial and the toasts escape their text."""

from __future__ import annotations

import pytest
from django.template import Context, Template
from django.urls import reverse

from accounts.roles import Role
from ledger.models import PaymentChannel
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator
from tests.test_lease_end_drafts import row_counts
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'


def a_world() -> tuple[object, object]:
    from datetime import date

    store = a_store(f"1991 Istanbul {MARKUP}", 1991)
    operator = an_operator(f"Fatih {MARKUP}")
    lease = a_lease(store, operator, channel=PaymentChannel.WIRE)
    charge = a_rent_charge(lease, date(2026, 10, 1))
    payment = a_payment(charge, "100.00", date(2026, 10, 6))
    return payment, lease


def test_the_void_and_refund_dialogs_are_reads_that_escape_and_refuse_a_viewer() -> None:
    payment, _ = a_world()
    admin, _ = client_for(Role.ADMIN)
    viewer, _ = client_for(Role.VIEWER)
    before = row_counts()
    for name in ("void", "refund"):
        url = reverse(f"ledger:payments:{name}", kwargs={"pk": payment.pk})  # type: ignore[attr-defined]
        response = admin.get(url)
        assert response.status_code == 200, name
        body = response.content.decode()
        assert (
            "<dialog" in body
            and " open" in body[body.index("<dialog") : body.index("<dialog") + 200]
        )
        assert MARKUP not in body and "Keep the payment" in body
        assert row_counts() == before, name
    for name in ("void", "refund"):
        url = reverse(f"ledger:payments:{name}", kwargs={"pk": payment.pk})  # type: ignore[attr-defined]
        assert viewer.get(url).status_code in (302, 403), name
        assert viewer.post(url, {"note": "x"}).status_code in (302, 403), name
    assert row_counts() == before


@pytest.mark.parametrize(
    "path",
    [
        "/no-such-page/",
        f"/stores/{MARKUP}/",
        "/stores/00000000-0000-0000-0000-000000000000/",
        "/stores/../../etc/passwd",
        "/%ZZ/",
    ],
)
def test_the_not_found_page_is_in_the_shell_and_leaks_nothing(path: str) -> None:
    billing, _ = client_for(Role.BILLING)
    response = billing.get(path)
    assert response.status_code in (400, 404)
    body = response.content.decode()
    assert MARKUP not in body
    assert "Traceback" not in body and "/Users/" not in body and "django" not in body.lower()
    if response.status_code == 404:
        assert "Go to the dashboard" in body
        assert 'id="page-header"' in body or "Not" in body


def test_the_forbidden_page_names_the_role_rule_and_offers_the_dashboard() -> None:
    _, lease = a_world()
    viewer, _ = client_for(Role.VIEWER)
    response = viewer.post(reverse("ledger:stores:create"), {"external_label": "x"})
    assert response.status_code in (302, 403)
    if response.status_code == 403:
        body = response.content.decode()
        assert "Your role does not allow this action." in body
        assert "Go to the dashboard" in body and "Traceback" not in body


def test_the_empty_state_partial_escapes_every_text() -> None:
    include = (
        '{% include "finance/_empty_state.html" with title=title text=text '
        "action_url=url action_label=label only %}"
    )
    rendered = Template(include).render(
        Context(
            {
                "title": f"No {MARKUP}",
                "text": f"Try {MARKUP}",
                "url": "/x/",
                "label": f"Go {MARKUP}",
            }
        )
    )
    assert MARKUP not in rendered
    assert rendered.count("&lt;script&gt;") == 3


def test_a_toast_escapes_its_text() -> None:
    """A flash message carrying a record's name with markup (the store's own flash)."""
    from tests.test_store_views import form_data

    admin, _ = client_for(Role.ADMIN)
    response = admin.post(
        reverse("ledger:stores:create"),
        form_data(external_label=f"Rome {MARKUP}", store_number="5151"),
        follow=True,
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert 'class="toast' in body and "added." in body
    assert MARKUP not in body and "Rome &lt;script&gt;" in body


# --- Slice E-1: Profit and loss ---------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "from=2026-13&to=2026-10",
        "from=2026-10&to=2020-01",
        "from=" + MARKUP + "&to=" + MARKUP,
        "from=2026-10&to=2026-10&group=" + MARKUP,
        "from=2026-10&to=2026-10&group=state",
        "from=2026-10&to=2026-10&detail=" + MARKUP,
        "from=1999-01&to=2026-10",
        "from=%00&to=%00",
    ],
)
def test_a_hostile_report_query_never_breaks_the_page_or_writes(query: str) -> None:
    a_world()
    billing, _ = client_for(Role.BILLING)
    before = row_counts()
    response = billing.get("/reports/pnl/?" + query)
    assert response.status_code == 200
    body = response.content.decode()
    assert MARKUP not in body and "Traceback" not in body
    assert 'id="page-header"' in body
    assert row_counts() == before


def test_the_report_escapes_a_stores_name_in_its_first_cell_and_the_viewer_may_read() -> None:
    from tests.test_pnl import a_cost

    payment, lease = a_world()
    a_cost(lease.store)  # type: ignore[attr-defined]
    viewer, _ = client_for(Role.VIEWER)
    before = row_counts()
    response = viewer.get("/reports/pnl/?from=2026-10&to=2026-10&detail=1")
    assert response.status_code == 200
    body = response.content.decode()
    assert MARKUP not in body and "Istanbul &lt;script&gt;" in body
    assert row_counts() == before


def test_a_store_without_a_cost_row_never_reads_zero_on_the_report() -> None:
    payment, lease = a_world()  # the store has no cost row
    billing, _ = client_for(Role.BILLING)
    body = billing.get("/reports/pnl/?from=2026-10&to=2026-10&detail=1").content.decode()
    assert "unknown" in body
    assert "Unknown: landlord rent" in body

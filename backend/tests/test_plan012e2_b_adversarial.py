"""Adversarial cases for PLAN-012e-2 Batch B (the three-action bar and the Lease menu), by the
QA owner (plan §6). A viewer sees no bar and no menu; billing sees no Lease menu and no End
lease link; the menu is a native disclosure that needs no script; the removed actions never
come back by id or label; the Create missing charges link keeps its role gate and its POST; a
vacant store offers New lease only; the operator page's More menu holds Archive as the
destructive item and nothing a viewer could post."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import Lease, PaymentChannel
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

REMOVED_IDS = ("charge-now", "add-setup-charge", "schedule-payment", "upload-document")
REMOVED_LABELS = (">Charge now<", ">Add setup charge<", ">Schedule a payment<", ">Upload document<")


def _bar(body: str) -> str:
    """The action bar's markup: from its opening tag to the first closing block tag."""
    start = body.index('class="action-bar')
    ends = [body.find(tag, start) for tag in ("</p>", "</div>", "</nav>")]
    end = min(index for index in ends if index != -1)
    return body[start:end]


def _leased_store() -> Lease:
    return a_lease(a_store("2601"), an_operator("Bar Co"), channel=PaymentChannel.WIRE)


# --- a viewer and billing --------------------------------------------------------------------


def test_a_viewer_sees_no_action_bar_and_no_menu() -> None:
    lease = _leased_store()
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id})
    ).content.decode()
    assert 'id="lease-menu"' not in body
    assert 'id="create-invoice"' not in body and 'id="record-payment"' not in body
    for removed in REMOVED_IDS:
        assert f'id="{removed}"' not in body, removed
    assert "End lease" not in body and "Transfer to another operator" not in body


def test_billing_sees_three_actions_but_no_lease_menu_and_no_end_lease_link() -> None:
    lease = _leased_store()
    billing, _ = client_for(Role.BILLING)
    body = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id})
    ).content.decode()
    assert 'id="create-invoice"' in body and 'id="record-payment"' in body
    assert 'id="lease-menu"' not in body
    assert reverse("ledger:stores:end_lease", kwargs={"pk": lease.store_id}) not in body
    for removed in REMOVED_IDS:
        assert f'id="{removed}"' not in body, removed
    for label in REMOVED_LABELS:
        assert label not in body, label


# --- the menu is a native disclosure --------------------------------------------------------


def test_the_lease_menu_is_a_details_element_with_three_items_and_a_danger_end() -> None:
    lease = _leased_store()
    admin, _ = client_for(Role.ADMIN)
    body = admin.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id})
    ).content.decode()
    start = body.index('id="lease-menu"')
    menu = body[body.rfind("<details", 0, start) : body.index("</details>", start)]
    assert "<summary" in menu and "Lease" in menu.split("</summary>")[0]
    links = re.findall(r'<a [^>]*href="([^"]+)"[^>]*>([^<]+)</a>', menu)
    labels = [label.strip() for _, label in links]
    assert labels == ["Edit lease", "Transfer to another operator", "End lease"], labels
    end_link = re.search(r'<a [^>]*href="[^"]+/end-lease/"[^>]*>', menu)
    assert end_link is not None and "menu-danger" in end_link.group(0)
    # Nothing in the menu is a form that a click could post by accident.
    assert "<form" not in menu
    # Exactly one bar, exactly three top-level controls on it.
    assert body.count('class="action-bar') == 1


def test_a_vacant_store_offers_new_lease_only_to_an_admin_and_nothing_to_billing() -> None:
    store = a_store("2602")
    admin, _ = client_for(Role.ADMIN)
    body = admin.get(reverse("ledger:stores:detail", kwargs={"pk": store.pk})).content.decode()
    assert 'id="new-lease"' in body
    assert 'id="lease-menu"' not in body and 'id="create-invoice"' not in body
    assert 'id="record-payment"' not in body
    billing, _ = client_for(Role.BILLING)
    body = billing.get(reverse("ledger:stores:detail", kwargs={"pk": store.pk})).content.decode()
    assert 'id="new-lease"' not in body and 'id="lease-menu"' not in body


# --- Create missing charges as a link keeps its gate --------------------------------------


def test_create_missing_charges_is_a_link_in_the_charges_tab_with_its_post_and_gate() -> None:
    lease = _leased_store()
    url = reverse("ledger:stores:create_charges", kwargs={"pk": lease.store_id})
    billing, _ = client_for(Role.BILLING)
    tab = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}), {"tab": "charges"}
    ).content.decode()
    assert url in tab
    form_start = tab.index(url)
    form = tab[tab.rfind("<form", 0, form_start) : tab.index("</form>", form_start)]
    assert 'method="post"' in form and "csrfmiddlewaretoken" in form
    assert "link-action" in form  # a link, not a button, by style
    # Not on the bar.
    assert 'class="action-bar' in tab
    assert url not in _bar(tab)
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(url, {}).status_code == 403
    assert viewer.get(url).status_code in (403, 405)


# --- the operator page's More menu ----------------------------------------------------------


def test_the_operator_more_menu_holds_archive_as_the_danger_item_and_a_viewer_sees_none() -> None:
    lease = _leased_store()
    assert lease.operator is not None
    admin, _ = client_for(Role.ADMIN)
    body = admin.get(
        reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id})
    ).content.decode()
    assert 'id="more-menu"' in body
    start = body.index('id="more-menu"')
    menu = body[body.rfind("<details", 0, start) : body.index("</details>", start)]
    assert "Archive" in menu and ("btn-danger" in menu or "menu-danger" in menu)
    # PLAN-012f-a D7 (DESIGN-SPEC §4 Operator workspace): New invoice leaves the operator page;
    # invoices are made from the store page.
    assert 'id="new-invoice"' not in body and 'id="statement"' in body
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get(
        reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id})
    ).content.decode()
    assert 'id="more-menu"' not in body and not has_token(body, "Archive")
    assert 'id="new-invoice"' not in body


def test_a_store_name_with_markup_in_the_bar_and_menu_is_escaped() -> None:
    lease = a_lease(
        a_store('26<script>alert("x")</script>'),
        an_operator("Escaped Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 9, 1),
    )
    admin, _ = client_for(Role.ADMIN)
    body = admin.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id})
    ).content.decode()
    assert '<script>alert("x")</script>' not in body
    assert 'id="lease-menu"' in body

"""PLAN-015f §3.2: the LEASE card's Status row is the badge (and, while paused, the reason
line) with no control; Pause / Resume collection, Activate now and Change start date are items
of the page's Lease ▾ menu beside Edit lease, Transfer and End lease, each only when it applies
and the role may take it."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import CollectionStatus, Lease
from ledger.services import leases as lease_service
from ledger.services import workspace
from payments.fake import FakeProvider
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import assert_absent

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


def a_world(status: str = CollectionStatus.ACTIVE, started: date = date(2026, 9, 1)) -> Lease:
    return a_lease(
        a_store("Menu 1515", 1515),
        an_operator("Menu Lease Co"),
        started=started,
        debit_day=1,
        status=status,
    )


def page(role: Role, lease: Lease) -> str:
    client: Client = client_for(role)[0]
    response = client.get(f"/stores/{lease.store_id}/")
    assert response.status_code == 200
    return response.content.decode()


def menu_ids(html: str) -> list[str]:
    """The Lease ▾ menu's item ids, in order; ``[]`` without the menu."""
    if 'id="lease-menu"' not in html:
        return []
    start = html.index('id="lease-menu"')
    menu = html[start : html.index("</details>", start)]
    return re.findall(r'<a role="menuitem" id="([^"]+)"', menu)


def status_row(html: str) -> str:
    start = html.index('id="header-status"')
    return html[start : html.index("</dd>", start)]


def lease_row(html: str) -> str:
    start = html.index('id="header-lease"')
    return html[start : html.index("</dd>", start)]


ADMIN_ACTIVE = ["bar-edit-lease", "pause-collection", "store-transfer", "store-end-lease"]


@pytest.mark.parametrize(
    ("role", "status", "started", "expected"),
    [
        # An active lease out of its start month.
        (Role.ADMIN, CollectionStatus.ACTIVE, date(2026, 9, 1), ADMIN_ACTIVE),
        (Role.BILLING, CollectionStatus.ACTIVE, date(2026, 9, 1), []),
        (Role.VIEWER, CollectionStatus.ACTIVE, date(2026, 9, 1), []),
        # An active lease in its start month: admin may change the start.
        (
            Role.ADMIN,
            CollectionStatus.ACTIVE,
            date(2026, 11, 1),
            [
                "bar-edit-lease",
                "change-start",
                "pause-collection",
                "store-transfer",
                "store-end-lease",
            ],
        ),
        (Role.BILLING, CollectionStatus.ACTIVE, date(2026, 11, 1), []),
        # Paused: Resume instead of Pause, admin only.
        (
            Role.ADMIN,
            CollectionStatus.PAUSED,
            date(2026, 9, 1),
            ["bar-edit-lease", "resume-collection", "store-transfer", "store-end-lease"],
        ),
        (Role.BILLING, CollectionStatus.PAUSED, date(2026, 9, 1), []),
        (Role.VIEWER, CollectionStatus.PAUSED, date(2026, 9, 1), []),
        # Prospective (no current lease): billing and admin change the start and activate.
        (
            Role.ADMIN,
            CollectionStatus.PROSPECTIVE,
            date(2026, 12, 1),
            ["change-start", "activate-lease"],
        ),
        (
            Role.BILLING,
            CollectionStatus.PROSPECTIVE,
            date(2026, 12, 1),
            ["change-start", "activate-lease"],
        ),
        (Role.VIEWER, CollectionStatus.PROSPECTIVE, date(2026, 12, 1), []),
        # Ended: nothing to act on.
        (Role.ADMIN, CollectionStatus.ENDED, date(2026, 9, 1), []),
        (Role.BILLING, CollectionStatus.ENDED, date(2026, 9, 1), []),
    ],
)
def test_the_lease_menu_items_per_role_and_state(
    role: Role, status: str, started: date, expected: list[str]
) -> None:
    lease = a_world(status, started)
    if status == CollectionStatus.PAUSED:
        Lease.objects.filter(pk=lease.pk).update(collection_status=CollectionStatus.ACTIVE)
        lease_service.pause_collection(lease.pk, reason="Closed for repairs.", actor=None)
    if status == CollectionStatus.ENDED:
        Lease.objects.filter(pk=lease.pk).update(ended_on=date(2026, 10, 31))
    html = page(role, lease)
    assert menu_ids(html) == expected
    if not expected:
        assert 'id="lease-menu"' not in html
    # No control on the card's Status and Lease rows, whatever the role.
    if 'id="header-status"' in html:
        assert "<a " not in status_row(html) and "<form" not in status_row(html)
    assert_absent(lease_row(html), "change-start")
    if role == Role.VIEWER:
        for gone in ("pause-collection", "resume-collection", "activate-lease", "change-start"):
            assert_absent(html, gone)
        assert "Pause collection" not in html and "Resume collection" not in html


def test_the_menu_items_keep_their_urls_and_labels() -> None:
    lease = a_world()
    found = workspace.store_workspace(lease.store, TODAY, Role.ADMIN).actions
    [menu] = [action for action in found if action.key == "lease"]
    assert [(item.label, item.url) for item in menu.items] == [
        ("Edit lease", f"/leases/{lease.pk}/edit/?store={lease.store_id}&lease={lease.pk}"),
        ("Pause collection", f"/leases/{lease.pk}/pause-collection/"),
        ("Transfer to another operator", f"/stores/{lease.store_id}/transfer/"),
        ("End lease", f"/stores/{lease.store_id}/end-lease/"),
    ]
    assert [item.danger for item in menu.items] == [False, False, False, True]


def test_an_admin_on_a_store_with_only_a_prospective_lease_gets_new_lease_and_the_menu() -> None:
    lease = a_world(CollectionStatus.PROSPECTIVE, date(2026, 12, 1))
    found = workspace.store_workspace(lease.store, TODAY, Role.ADMIN).actions
    assert [action.key for action in found] == ["new_lease", "lease"]
    billing = workspace.store_workspace(lease.store, TODAY, Role.BILLING).actions
    assert [action.key for action in billing] == ["lease"]
    assert [item.key for item in billing[0].items] == ["change_start", "activate"]


def test_the_paused_reason_line_stays_under_the_badge() -> None:
    lease = a_world()
    lease_service.pause_collection(lease.pk, reason="Closed for repairs.", actor=None)
    row = status_row(page(Role.ADMIN, lease))
    assert 'id="pause-reason">Paused since ' in row and "Closed for repairs." in row


def test_billing_still_may_not_pause_by_the_url() -> None:
    lease = a_world()
    client = client_for(Role.BILLING)[0]
    assert client.post(f"/leases/{lease.pk}/pause-collection/", {"reason": "x"}).status_code == 403
    assert Lease.objects.get(pk=lease.pk).collection_status == CollectionStatus.ACTIVE


def test_the_operator_page_bar_carries_the_same_menu() -> None:
    lease = a_world()
    client = client_for(Role.ADMIN)[0]
    assert lease.operator is not None
    html = client.get(f"/operators/{lease.operator.pk}/?store={lease.store_id}").content.decode()
    assert menu_ids(html) == ADMIN_ACTIVE
    assert "<a " not in status_row(html)

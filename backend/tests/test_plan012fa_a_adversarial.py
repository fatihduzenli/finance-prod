"""Adversarial cases for PLAN-012f-a Batch A, part D (the statement step as a pure read), by
the QA owner (plan §6). A GET and a HEAD of a lease-end statement step, and of every page the
visual suite shoots, change no row of any table and write no audit row, for an admin and for
billing; the preview line names the prorated amount with no link and reads "not created yet";
the save by a viewer or billing is refused and writes nothing; a hostile deductions post does
not create the charges when the offset rule refuses it (one transaction)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.apps import apps
from django.db.models import Model
from django.test import Client
from django.urls import reverse

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from ledger.models import AuditLog, DeductionKind, LeaseEnd, RentCharge
from ledger.services import lease_end
from payments.fake import FakeProvider
from tests.test_operator_views import client_for
from tests.visual import world as visual_world
from tests.visual.test_pages import AS_ADMIN, PAGES, SIGNED_OUT

pytestmark = pytest.mark.django_db


def row_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in apps.get_models():
        assert issubclass(model, Model)
        if model._meta.app_label in {"ledger", "payments", "accounts", "documents"}:
            counts[model._meta.label] = model._default_manager.count()
    return counts


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> visual_world.World:
    clock = visual_world.FrozenClock()
    monkeypatch.setattr("django.utils.timezone.now", clock)
    monkeypatch.setattr("payments.get_provider", FakeProvider)
    monkeypatch.setattr(documents_storage, "get_storage", MemoryDocumentStorage)
    world = visual_world.build()
    clock.stop()
    return world


class _Site:
    def __init__(self, world: visual_world.World) -> None:
        self.world = world


def test_a_get_and_a_head_of_every_page_in_the_suite_write_nothing(
    built: visual_world.World,
) -> None:
    admin, _ = client_for(Role.ADMIN)
    billing, _ = client_for(Role.BILLING)
    site: Any = _Site(built)
    before = row_counts()
    audit_before = AuditLog.objects.count()
    anonymous = Client()
    for name, path_of in PAGES.items():
        client = anonymous if name in SIGNED_OUT else admin if name in AS_ADMIN else billing
        path = path_of(site)
        for method in (client.get, client.head):
            response = method(path)
            assert response.status_code == 200, (name, method.__name__, response.status_code)
    assert row_counts() == before
    assert AuditLog.objects.count() == audit_before


def test_the_statement_step_renders_the_preview_and_writes_nothing(
    built: visual_world.World,
) -> None:
    admin, _ = client_for(Role.ADMIN)
    path = reverse("ledger:lease_ends:statement", kwargs={"pk": built.lease_end.pk})
    before = row_counts()
    body = admin.get(path).content.decode()
    assert row_counts() == before
    assert "1,225.81" in body  # 19 of 31 days of 2,000.00: the end month's preview
    assert "not created yet" in body
    assert not RentCharge.objects.filter(
        lease=built.second_lease, period_month=date(2026, 10, 1)
    ).exists()
    end = LeaseEnd.objects.get(pk=built.lease_end.pk)
    assert end.statement_opened_at is None


def test_a_viewer_or_billing_cannot_save_the_statement_and_nothing_is_written(
    built: visual_world.World,
) -> None:
    path = reverse("ledger:lease_ends:statement", kwargs={"pk": built.lease_end.pk})
    clients = {role: client_for(role)[0] for role in (Role.VIEWER, Role.BILLING)}
    before = row_counts()  # after the sign-ins: their session rows are not the page's writes
    for role, client in clients.items():
        response = client.post(path, {"lines-TOTAL_FORMS": "0", "lines-INITIAL_FORMS": "0"})
        assert response.status_code in (302, 403), (role, response.status_code)
        if response.status_code == 302:
            assert "/account/" in response["Location"] or "/stores/" in response["Location"]
    assert row_counts() == before
    assert Client().post(path).status_code in (302, 401, 403)
    assert row_counts() == before


def test_a_refused_save_creates_no_charge(built: visual_world.World) -> None:
    """The offset may not exceed the open balance: a hostile post that trips the rule leaves
    the end month's charge uncreated (one transaction, nothing half done)."""
    end = LeaseEnd.objects.get(pk=built.lease_end.pk)
    before = row_counts()
    outcome = lease_end.save_statement(
        end,
        [
            lease_end.DeductionInput(
                DeductionKind.UNPAID_BALANCE, "offset above the balance", Decimal("999999.00")
            )
        ],
        actor=built.user,
        today=visual_world.TODAY,
    )
    assert not outcome.ok
    assert row_counts() == before
